"""The swing book's desk page: triggers, plan, book, and the one button (SW7, the page half).

`docs/swing/05` §3: "One page, three panels, refreshed every 5 s during 09:15–10:45 and on
demand otherwise." Triggers on top — today's `sw_signal` rows, newest first, each with the line
it became and a **Confirm** per line. The plan in the middle — the morning plan and the EOD
preview, exits first, then the buys waiting for a signal, then the skips with their reasons.
The book at the bottom — open positions with their GTT ids, **Re-arm GTT** for a naked one, the
last five manage actions. A status bar: `DRY_RUN`, `BASKFY_SWING_EXECUTION_ENABLED`, the
monitor's state, the Kite token's age, the session's counters — and, since SW15, the last
detection scan ("provisional — scanned 13:42 IST from live quotes") with a **Scan now** button.

SCAN NOW (SW15)
---------------
The desk has no Celery client — its venv carries none and it reaches Baskfy through Postgres
alone — so `POST /swing/scan` writes one `sw_scan_run` row (`QUEUED`, `source=desk`) and the
worker's minute sweep publishes it (`baskfy.swing.scan_sweep`). The two rules the API applies
are applied here from the same table: one in flight per user (409) and one a minute (429).
A scan reads quotes and writes detection rows; it is not an order and it is not a confirm.

WHAT THIS MODULE IS, AND IS NOT
--------------------------------
It is a page and a store. It reads the `sw_` tables for one user and renders them; it takes a
form post and hands it to `app.swing_execute.execute_line`, which is the only thing in the desk
that knows what a confirmed swing line does. Nothing in this file names a broker call or an
order verb — `tests/test_swing_desk.py` strips the docstrings and asserts it — because a page
that can place is a page that can be talked into placing. The route is the single doorway to
`execute_line`, and `execute_line` is the single doorway to the gateway (the second law).

THE STORE
---------
`PgSwingStore` is the `SwingStore` of contract C1: one user's rows, `?` placeholders, tables
qualified by schema. On the desk's Postgres the connection sits on ``search_path=desk`` and the
swing book lives in ``public`` (`app.swing_monitor` does the same); in tests the schema is
``""`` and the same SQL runs against a sqlite file built from the tests' own DDL. Every price
crosses the boundary as `Decimal` — in through `_bind`, which also rounds at the schema's scale
(house rule 8), and out through `_dec`, because the Postgres adapter hands NUMERIC back as
`float` and sqlite hands it back as whatever it stored.

THE VIEW
--------
`build_view` is pure over the store and the clock: it takes ``now`` rather than reading it, so
a test can render 09:31 on a Tuesday. It never raises on an empty book — a desk with nothing to
confirm still needs to say so, and say why.

THE LOCK (SW10.4, STANDING-ANSWERS A5)
--------------------------------------
`lock_session_for_update` is the store's half of the confirm-time gate: one transaction per
confirm, holding the day's ``sw_session`` row (``SELECT … FOR UPDATE``; ``BEGIN IMMEDIATE`` on
the sqlite twin, which has no row locks) from before the book is re-derived until after the
gateway has answered and the rows are written. The desk's connections are autocommit, so the
transaction is opened and closed here explicitly; an exception rolls it back, and
`execute_line` then records the refusal outside it. `session_context` is
`app.swing_monitor.load_context` over this store's schema — the same reading the monitor sizes
a SIGNAL line against, so the page's preview and the confirm's re-size agree.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import decimal
import enum
import hashlib
import json
import logging
import sqlite3
import uuid
from collections.abc import Iterator, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from baskfy_core.swing.config import DEFAULT_SWING_CONFIG
from baskfy_core.swing.opening_range import TriggerState
from baskfy_core.swing.plan import first_live_multiplier
from baskfy_core.swing.plan import EXECUTABLE_KINDS as _EXECUTABLE_KINDS
from baskfy_core.swing.plan import LineKind

from . import config as C
from .core.guards import UntouchableInstrumentError
from .swing_monitor import IST, SCHEMA, SignalContext, load_context

log = logging.getLogger("swing_desk")

router = APIRouter()

#: The plan-line kinds that act on a position the book already holds, in the order the plan
#: panel lists them (`05` §3: "exit lines first (SELL at open, RAISE GTT)").
EXIT_KINDS: tuple[str, ...] = (LineKind.SELL_AT_OPEN.value, LineKind.RAISE_GTT_STOP.value)
#: A7 (SW10.5): the kinds a Confirm may post. `PENDING_RANGE` is not one, and the route refuses
#: it with a 400 before `execute_line` is even called — the same set the execute module keeps.
EXECUTABLE_KINDS: frozenset[str] = frozenset(k.value for k in _EXECUTABLE_KINDS)
PENDING_KIND: str = LineKind.PENDING_RANGE.value
#: The only line state a Confirm button may be attached to.
PROPOSED = "PROPOSED"
#: How many manage actions the book panel shows (`05` §3: "the last five").
MANAGE_ACTIONS_SHOWN = 5
#: The page polls `/swing/data` this often inside the monitor window, and not at all outside it.
POLL_MS = 5_000

#: Storage scales from `alembic/versions/0028_swing.py`, applied on the way in (house rule 8).
_SCALE_2 = Decimal("0.01")
_SCALE_4 = Decimal("0.0001")
_COLUMN_SCALE: dict[str, Decimal] = {
    "entry_avg": _SCALE_4,
    "exit_avg": _SCALE_4,
    "price": _SCALE_4,
    "initial_stop": _SCALE_2,
    "stop": _SCALE_2,
    "gtt_trigger": _SCALE_2,
    "trigger": _SCALE_2,
    "r_multiple": _SCALE_2,
    "pnl_inr": _SCALE_2,
    "risk_inr": _SCALE_2,
    "position_value": _SCALE_2,
    "range_high": _SCALE_2,
    "range_low": _SCALE_2,
    "low_of_day": _SCALE_2,
    "last_price": _SCALE_2,
    "entry": _SCALE_2,
    "total_risk_inr": _SCALE_2,
    "total_new_exposure_inr": _SCALE_2,
    "sleeve_capital_inr": _SCALE_2,
    "max_position_pct": _SCALE_2,
    "risk_per_trade_pct": Decimal("0.001"),
}

#: The columns a caller may write on `sw_position`. Anything else in `fields` is a bug in the
#: caller and is refused loudly rather than interpolated into SQL.
_POSITION_COLUMNS: frozenset[str] = frozenset(
    {
        "broker_account_id", "instrument_id", "setup", "entry_date", "entry_avg",
        "quantity_entered", "initial_stop", "stop", "gtt_id", "gtt_trigger", "gtt_armed_at",
        "trail", "partial_done", "partial_date", "quantity_open", "state", "closed_on",
        "exit_avg", "close_reason", "r_multiple", "pnl_inr", "simulated", "half_risk",
    }
)
_FILL_COLUMNS: frozenset[str] = frozenset(
    {"position_id", "side", "quantity", "price", "filled_at", "journal_ref", "simulated"}
)
#: Keys a caller may pass with a row that are not columns of it (they come from a join).
_NOT_COLUMNS: frozenset[str] = frozenset({"symbol", "id"})


# --- values across the boundary ------------------------------------------------------------


def _dec(value: Any, column: str = "") -> Decimal | None:
    """A NUMERIC column as `Decimal`, whatever the driver made of it.

    The Postgres adapter returns `float` (its `_native`, for the desk's float ledger); sqlite
    returns `float` for a NUMERIC-affinity column and `str` for a TEXT one. `Decimal(str(x))`
    is exact for every value the schema can hold — the scales are 2 and 4, the precision 18 —
    and it is the same conversion `app.swing_monitor` uses. The float detour drops trailing
    zeros (100.80 comes back as 100.8), so a known column is re-quantised to its storage
    scale: the number the page shows is the number the schema holds, not the driver's spelling.
    """
    if value is None:
        return None
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    scale = _COLUMN_SCALE.get(column)
    return number.quantize(scale) if scale is not None else number


def _date(value: Any) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def _stamp(value: Any) -> dt.datetime | None:
    """A timestamptz as a tz-aware IST datetime. A naive value is taken to be IST — the desk's
    clock — because every stamp the swing book writes carries its offset. The one exception is
    sqlite's `CURRENT_TIMESTAMP` (UTC, naive) on `updated_at`, which the store uses only to
    order the manage list and never shows."""
    if value is None:
        return None
    moment = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value).strip())
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=IST)
    return moment.astimezone(IST)


def _bind(value: Any, column: str = "") -> Any:  # noqa: ANN401 - a driver parameter
    """A Python value as both drivers accept it.

    psycopg dumps `str` with the *unknown* OID, so an ISO date, an ISO timestamp, a decimal
    string and a uuid string all land in their typed columns; sqlite stores the same strings
    and reads them back as text or REAL. Sending strings for the typed values is therefore the
    one representation that needs no branch on the backend. Decimals are quantised to the
    column's storage scale first: the stored number is the contract (house rule 8).
    """
    if isinstance(value, enum.Enum):
        value = value.value
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        scale = _COLUMN_SCALE.get(column)
        if scale is not None:
            value = value.quantize(scale, rounding=decimal.ROUND_HALF_UP)
        return format(value, "f")
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=IST)
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _json_column(value: Any) -> Any:  # noqa: ANN401 - JSONB comes back as dict or text
    """A JSONB column as Python: psycopg hands back a dict, sqlite the text it stored."""
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except ValueError:
        return None


class ScanRefused(Exception):
    """`request_scan`'s two refusals, carrying the HTTP status the API answers with."""

    def __init__(self, status: int, reason: str, *, run_id: int, retry_after: int | None = None):
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.run_id = run_id
        self.retry_after = retry_after


def _jsonable(value: Any) -> Any:  # noqa: ANN401 - the whole point is "any value"
    """The view as JSON can carry it: Decimal → str (never float — it is money), dates → ISO."""
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, enum.Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable(dataclasses.asdict(value))
    return value


# --- the store ----------------------------------------------------------------------------


class PgSwingStore:
    """One user's `sw_` rows, through a sqlite3-shaped connection (contract C1 `SwingStore`).

    ``schema="public"`` on the desk's Postgres, ``""`` for a sqlite file in tests. Beyond the
    protocol's methods it carries the reads the page needs (`signals_for`, `plans_for`,
    `open_positions`, `recent_manage_actions`, `session`); those are this module's, not the
    contract's, and `app.swing_execute` must not depend on them.
    """

    def __init__(
        self,
        conn: Any,
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
        """`schema.table`, or the bare name when the schema is empty (sqlite)."""
        return f"{self.schema}.{table}" if self.schema else table

    # -- SwingStore: plans and lines ------------------------------------------------------

    def plan(self, plan_id: str) -> dict | None:
        """None for an unknown plan — including a malformed id: `sw_plan.plan_id` is a uuid
        column on Postgres, and a form post that is not one would otherwise be a driver error
        (a 500) where the contract says 404."""
        try:
            uuid.UUID(str(plan_id))
        except ValueError:
            return None
        row = self.conn.execute(
            f"SELECT id, plan_id, as_of, source, built_at, expires_at, gate, exposure_level, "
            f"total_risk_inr, total_new_exposure_inr FROM {self.t('sw_plan')} "
            "WHERE user_id = ? AND plan_id = ?",
            (self.user_id, str(plan_id)),
        ).fetchone()
        return self._plan_row(row) if row is not None else None

    def _plan_row(self, row: Any) -> dict:
        return {
            "id": int(row["id"]),
            "plan_id": str(row["plan_id"]),
            "as_of": _date(row["as_of"]),
            "source": str(row["source"]),
            "built_at": _stamp(row["built_at"]),
            "expires_at": _stamp(row["expires_at"]),
            "gate": str(row["gate"]),
            "exposure_level": int(row["exposure_level"]),
            "total_risk_inr": _dec(row["total_risk_inr"], "total_risk_inr"),
            "total_new_exposure_inr": _dec(row["total_new_exposure_inr"], "total_new_exposure_inr"),
        }

    _LINE_SELECT = (
        "SELECT l.id, l.plan_id AS plan_pk, p.plan_id, l.kind, l.instrument_id, i.symbol, "
        "l.setup, l.quantity, l.trigger, l.stop, l.risk_inr, l.position_value, l.trail, "
        "l.note, l.state, l.client_id, l.journal_ref, l.position_id, l.updated_at, "
        "p.as_of, p.source AS plan_source "
    )

    def _line_from(self) -> str:
        return (
            f"FROM {self.t('sw_plan_line')} l "
            f"JOIN {self.t('sw_plan')} p ON p.id = l.plan_id "
            f"JOIN {self.t('instrument')} i ON i.id = l.instrument_id "
        )

    def line(self, line_id: int) -> dict | None:
        row = self.conn.execute(
            self._LINE_SELECT + self._line_from() + "WHERE l.user_id = ? AND l.id = ?",
            (self.user_id, int(line_id)),
        ).fetchone()
        return self._line_row(row) if row is not None else None

    def _line_row(self, row: Any) -> dict:
        return {
            "id": int(row["id"]),
            "plan_pk": int(row["plan_pk"]),
            "plan_id": str(row["plan_id"]),
            "kind": str(row["kind"]),
            "instrument_id": int(row["instrument_id"]),
            "symbol": str(row["symbol"]),
            "setup": str(row["setup"]) if row["setup"] is not None else None,
            "quantity": int(row["quantity"]),
            "trigger": _dec(row["trigger"], "trigger"),
            "stop": _dec(row["stop"], "stop"),
            "risk_inr": _dec(row["risk_inr"], "risk_inr"),
            "position_value": _dec(row["position_value"], "position_value"),
            "trail": str(row["trail"]) if row["trail"] is not None else None,
            "note": str(row["note"]) if row["note"] is not None else None,
            "state": str(row["state"]),
            "client_id": str(row["client_id"]),
            "journal_ref": str(row["journal_ref"]) if row["journal_ref"] is not None else None,
            "position_id": int(row["position_id"]) if row["position_id"] is not None else None,
            "updated_at": _stamp(row["updated_at"]),
            "as_of": _date(row["as_of"]),
            "plan_source": str(row["plan_source"]),
        }

    def set_line(
        self,
        line_id: int,
        *,
        state: str,
        journal_ref: str | None = None,
        position_id: int | None = None,
    ) -> None:
        """The state always; the journal reference and the position only when given, so a
        later state change (SENT → FILLED) cannot blank what an earlier one recorded."""
        sets = ["state = ?"]
        params: list[Any] = [_bind(state)]
        if journal_ref is not None:
            sets.append("journal_ref = ?")
            params.append(str(journal_ref))
        if position_id is not None:
            sets.append("position_id = ?")
            params.append(int(position_id))
        sets.append("updated_at = CURRENT_TIMESTAMP")
        params += [self.user_id, int(line_id)]
        self.conn.execute(
            f"UPDATE {self.t('sw_plan_line')} SET {', '.join(sets)} WHERE user_id = ? AND id = ?",
            params,
        )

    # -- SwingStore: positions and fills --------------------------------------------------

    _POSITION_SELECT = (
        "SELECT p.id, p.instrument_id, i.symbol, p.setup, p.entry_date, p.entry_avg, "
        "p.quantity_entered, p.quantity_open, p.initial_stop, p.stop, p.gtt_id, p.gtt_trigger, "
        "p.gtt_armed_at, p.trail, p.partial_done, p.partial_date, p.state, p.closed_on, "
        "p.exit_avg, p.close_reason, p.r_multiple, p.pnl_inr, p.simulated, p.half_risk "
    )

    def _position_from(self) -> str:
        return (
            f"FROM {self.t('sw_position')} p "
            f"JOIN {self.t('instrument')} i ON i.id = p.instrument_id "
        )

    def _position_row(self, row: Any) -> dict:
        return {
            "id": int(row["id"]),
            "instrument_id": int(row["instrument_id"]),
            "symbol": str(row["symbol"]),
            "setup": str(row["setup"]),
            "entry_date": _date(row["entry_date"]),
            "entry_avg": _dec(row["entry_avg"], "entry_avg"),
            "quantity_entered": int(row["quantity_entered"]),
            "quantity_open": int(row["quantity_open"]),
            "initial_stop": _dec(row["initial_stop"], "initial_stop"),
            "stop": _dec(row["stop"], "stop"),
            "gtt_id": str(row["gtt_id"]) if row["gtt_id"] is not None else None,
            "gtt_trigger": _dec(row["gtt_trigger"], "gtt_trigger"),
            "gtt_armed_at": _stamp(row["gtt_armed_at"]),
            "trail": str(row["trail"]),
            "partial_done": bool(row["partial_done"]),
            "partial_date": _date(row["partial_date"]),
            "state": str(row["state"]),
            "closed_on": _date(row["closed_on"]),
            "exit_avg": _dec(row["exit_avg"], "exit_avg"),
            "close_reason": str(row["close_reason"]) if row["close_reason"] is not None else None,
            "r_multiple": _dec(row["r_multiple"], "r_multiple"),
            "pnl_inr": _dec(row["pnl_inr"], "pnl_inr"),
            "simulated": bool(row["simulated"]),
            "half_risk": bool(row["half_risk"]),
        }

    def open_position_for(self, instrument_id: int) -> dict | None:
        """The OPEN/PARTIAL position with shares still on the book for this name, or None."""
        row = self.conn.execute(
            self._POSITION_SELECT
            + self._position_from()
            + "WHERE p.user_id = ? AND p.instrument_id = ? AND p.state IN ('OPEN', 'PARTIAL') "
            "AND p.quantity_open > 0 ORDER BY p.id DESC LIMIT 1",
            (self.user_id, int(instrument_id)),
        ).fetchone()
        return self._position_row(row) if row is not None else None

    def position(self, position_id: int) -> dict | None:
        row = self.conn.execute(
            self._POSITION_SELECT + self._position_from() + "WHERE p.user_id = ? AND p.id = ?",
            (self.user_id, int(position_id)),
        ).fetchone()
        return self._position_row(row) if row is not None else None

    def create_position(self, fields: dict) -> int:
        """`fields` may carry `symbol` — `execute_line` passes the line's — and it is dropped:
        the row keys the instrument by id, and the symbol is the join's, not the table's."""
        values = {k: v for k, v in fields.items() if k not in _NOT_COLUMNS}
        unknown = set(values) - _POSITION_COLUMNS
        if unknown:
            raise ValueError(f"sw_position has no column {sorted(unknown)}")
        values.setdefault("broker_account_id", self.broker_account_id)
        values.setdefault("quantity_open", values.get("quantity_entered"))
        values.setdefault("state", "OPEN")
        values.setdefault("simulated", True)
        values.setdefault("partial_done", False)
        columns = ["user_id", *values]
        params = [self.user_id, *(_bind(v, k) for k, v in values.items())]
        row = self.conn.execute(
            f"INSERT INTO {self.t('sw_position')} ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)}) RETURNING id",
            params,
        ).fetchone()
        return int(row["id"])

    def update_position(self, position_id: int, fields: dict) -> None:
        unknown = set(fields) - _POSITION_COLUMNS
        if unknown:
            raise ValueError(f"sw_position has no column {sorted(unknown)}")
        if not fields:
            return
        sets = [f"{k} = ?" for k in fields] + ["updated_at = CURRENT_TIMESTAMP"]
        params = [*(_bind(v, k) for k, v in fields.items()), self.user_id, int(position_id)]
        self.conn.execute(
            f"UPDATE {self.t('sw_position')} SET {', '.join(sets)} WHERE user_id = ? AND id = ?",
            params,
        )

    def add_fill(self, fields: dict) -> int:
        unknown = set(fields) - _FILL_COLUMNS
        if unknown:
            raise ValueError(f"sw_fill has no column {sorted(unknown)}")
        values = dict(fields)
        values.setdefault("simulated", True)
        columns = ["user_id", *values]
        params = [self.user_id, *(_bind(v, k) for k, v in values.items())]
        row = self.conn.execute(
            f"INSERT INTO {self.t('sw_fill')} ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)}) RETURNING id",
            params,
        ).fetchone()
        return int(row["id"])

    def fills_for(self, position_id: int) -> list[dict]:
        rows = self.conn.execute(
            f"SELECT id, side, quantity, price, filled_at, journal_ref, simulated "
            f"FROM {self.t('sw_fill')} WHERE user_id = ? AND position_id = ? ORDER BY id",
            (self.user_id, int(position_id)),
        ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "side": str(r["side"]),
                "quantity": int(r["quantity"]),
                "price": _dec(r["price"], "price"),
                "filled_at": _stamp(r["filled_at"]),
                "journal_ref": str(r["journal_ref"]) if r["journal_ref"] is not None else None,
                "simulated": bool(r["simulated"]),
            }
            for r in rows
        ]

    # -- SwingStore: the session and the config --------------------------------------------

    def bump_session(
        self,
        day: dt.date,
        *,
        mode: str,
        confirms: int = 0,
        fills: int = 0,
        manage_actions: int = 0,
    ) -> None:
        """Upsert on (user_id, session_date), adding to the counters — the row may already
        exist (the monitor marks it at 10:45, the evening job writes the rest) or not yet."""
        self.conn.execute(
            f"INSERT INTO {self.t('sw_session')} AS s (user_id, session_date, mode, monitor_ran, "
            "signals, confirms, fills, manage_actions, notes) "
            "VALUES (?, ?, ?, false, 0, ?, ?, ?, 'swing-desk') "
            "ON CONFLICT (user_id, session_date) DO UPDATE SET "
            "confirms = s.confirms + EXCLUDED.confirms, fills = s.fills + EXCLUDED.fills, "
            "manage_actions = s.manage_actions + EXCLUDED.manage_actions, mode = EXCLUDED.mode",
            (self.user_id, _bind(day), str(mode), int(confirms), int(fills), int(manage_actions)),
        )

    def note_session(self, day: dt.date, note: str) -> None:
        """SW11: append one line to the day's `sw_session.notes` — what the clock did (the
        10:45 cutoff, the 15:15 sweep). The row is created if the day has none yet."""
        self.conn.execute(
            f"INSERT INTO {self.t('sw_session')} AS s (user_id, session_date, mode, monitor_ran, "
            "signals, confirms, fills, manage_actions, notes) "
            "VALUES (?, ?, ?, false, 0, 0, 0, 0, ?) "
            "ON CONFLICT (user_id, session_date) DO UPDATE SET "
            "notes = COALESCE(s.notes, '') || ?",
            (self.user_id, _bind(day), "DRY_RUN", str(note), "\n" + str(note)),
        )

    def session(self, day: dt.date) -> dict | None:
        row = self.conn.execute(
            f"SELECT session_date, mode, monitor_ran, signals, confirms, fills, manage_actions, "
            f"notes FROM {self.t('sw_session')} WHERE user_id = ? AND session_date = ?",
            (self.user_id, _bind(day)),
        ).fetchone()
        if row is None:
            return None
        return {
            "session_date": _date(row["session_date"]),
            "mode": str(row["mode"]),
            "monitor_ran": bool(row["monitor_ran"]),
            "signals": int(row["signals"]),
            "confirms": int(row["confirms"]),
            "fills": int(row["fills"]),
            "manage_actions": int(row["manage_actions"]),
            "notes": str(row["notes"]) if row["notes"] is not None else None,
        }

    def config(self) -> dict:
        """The sleeve's money and the two counters. A user without a row gets the schema's own
        server defaults — zero capital, 0.5 %, five half-risk sessions, rung 0 — which is what
        the migration would have seeded, and a sleeve with no capital plans nothing."""
        row = self.conn.execute(
            f"SELECT sleeve_capital_inr, risk_per_trade_pct, max_position_pct, "
            f"max_open_positions, first_live_sessions_left, exposure_level "
            f"FROM {self.t('sw_config')} WHERE user_id = ?",
            (self.user_id,),
        ).fetchone()
        if row is None:
            sizing = DEFAULT_SWING_CONFIG.sizing
            return {
                "sleeve_capital_inr": Decimal(0),
                "risk_per_trade_pct": Decimal(str(sizing.risk_per_trade_pct)),
                "max_position_pct": Decimal(str(sizing.max_position_pct)),
                "max_open_positions": sizing.max_open_positions,
                "first_live_sessions_left": 5,
                "exposure_level": 0,
                "present": False,
            }
        return {
            "sleeve_capital_inr": _dec(row["sleeve_capital_inr"], "sleeve_capital_inr"),
            "risk_per_trade_pct": _dec(row["risk_per_trade_pct"], "risk_per_trade_pct"),
            "max_position_pct": _dec(row["max_position_pct"], "max_position_pct"),
            "max_open_positions": int(row["max_open_positions"]),
            "first_live_sessions_left": int(row["first_live_sessions_left"]),
            "exposure_level": int(row["exposure_level"]),
            "present": True,
        }

    def set_first_live_sessions_left(self, value: int) -> None:
        self.conn.execute(
            f"UPDATE {self.t('sw_config')} SET first_live_sessions_left = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (max(int(value), 0), self.user_id),
        )

    # -- SwingStore: the confirm-time gate (SW10.4) ----------------------------------------

    @property
    def _sqlite(self) -> bool:
        return isinstance(self.conn, sqlite3.Connection)

    @contextlib.contextmanager
    def lock_session_for_update(self, day: dt.date) -> Iterator[None]:
        """One confirm, one transaction, the day's session row locked throughout.

        The row is inserted first if the day has none (the monitor and the evening upsert it
        too; `ON CONFLICT DO NOTHING` keeps theirs), then selected `FOR UPDATE`: a second
        confirm of the same session blocks here until this one commits, and then re-reads a
        book that includes it. sqlite has no row lock — `BEGIN IMMEDIATE` takes the database's
        write lock, which serialises the same way for a one-file desk. The connections are
        autocommit (`analytics.db.connect`, `analytics.pg.Connection`), so `BEGIN` / `COMMIT`
        / `ROLLBACK` are issued as statements; nothing else in this module opens a transaction.
        """
        self.conn.execute("BEGIN IMMEDIATE" if self._sqlite else "BEGIN")
        try:
            self.conn.execute(
                f"INSERT INTO {self.t('sw_session')} (user_id, session_date, mode, monitor_ran, "
                "signals, confirms, fills, manage_actions, notes) "
                "VALUES (?, ?, ?, false, 0, 0, 0, 0, 'swing-desk') "
                "ON CONFLICT (user_id, session_date) DO NOTHING",
                (self.user_id, _bind(day), "DRY_RUN" if C.DRY_RUN or not C.SWING_EXECUTION_ENABLED
                 else "LIVE"),
            )
            if not self._sqlite:
                self.conn.execute(
                    f"SELECT session_date FROM {self.t('sw_session')} "
                    "WHERE user_id = ? AND session_date = ? FOR UPDATE",
                    (self.user_id, _bind(day)),
                )
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def session_context(self, day: dt.date) -> SignalContext:
        """`load_context` over this store's schema: what the confirm is re-sized against."""
        return load_context(self.conn, user_id=self.user_id, day=day, schema=self.schema)

    def resize_line(
        self,
        line_id: int,
        *,
        quantity: int,
        risk_inr: Decimal,
        position_value: Decimal,
        note: str,
    ) -> None:
        """A BUY line shrunk at confirm (SW10.4): the three numbers the size decides, and the
        note saying what was done, so the row is the record of what went out."""
        self.conn.execute(
            f"UPDATE {self.t('sw_plan_line')} SET quantity = ?, risk_inr = ?, position_value = ?, "
            "note = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND id = ?",
            (int(quantity), _bind(risk_inr, "risk_inr"), _bind(position_value, "position_value"),
             str(note), self.user_id, int(line_id)),
        )

    # -- SW10.5 (A7, A8): the marketable limit, the late fill, the cutoff ------------------

    def range_high_for(self, line_id: int) -> Decimal | None:
        """The opening-range high of the signal that became this line, for the marketable
        limit (A8); None for a line no signal produced (an EOD entry reads the trigger)."""
        row = self.conn.execute(
            f"SELECT range_high FROM {self.t('sw_signal')} "
            "WHERE user_id = ? AND plan_line_id = ? ORDER BY id DESC LIMIT 1",
            (self.user_id, int(line_id)),
        ).fetchone()
        return _dec(row["range_high"], "range_high") if row is not None else None

    def line_by_order(self, order_id: str) -> dict | None:
        """The SENT BUY line whose `journal_ref` is this broker order id (A8's postback)."""
        row = self.conn.execute(
            self._LINE_SELECT + self._line_from()
            + "WHERE l.user_id = ? AND l.kind = 'BUY_ON_TRIGGER' AND l.state = 'SENT' "
            "AND l.journal_ref = ? ORDER BY l.id DESC LIMIT 1",
            (self.user_id, str(order_id)),
        ).fetchone()
        return self._line_row(row) if row is not None else None

    def sent_buy_lines(self, day: dt.date) -> list[dict]:
        rows = self.conn.execute(
            self._LINE_SELECT + self._line_from()
            + "WHERE l.user_id = ? AND p.as_of = ? AND l.kind = 'BUY_ON_TRIGGER' "
            "AND l.state = 'SENT' ORDER BY l.id",
            (self.user_id, _bind(day)),
        ).fetchall()
        return [self._line_row(r) for r in rows]

    def note_line(self, line_id: int, note: str) -> None:
        self.conn.execute(
            f"UPDATE {self.t('sw_plan_line')} SET note = COALESCE(note || '; ', '') || ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND id = ?",
            (str(note), self.user_id, int(line_id)),
        )

    def expire_pending(self, day: dt.date) -> int:
        """A7: every PENDING_RANGE line of `day` still PROPOSED → EXPIRED (the 10:45 sweep)."""
        rows = self.conn.execute(
            f"UPDATE {self.t('sw_plan_line')} SET state = 'EXPIRED', "
            "note = COALESCE(note || '; ', '') || 'slot freed at 10:45', "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE user_id = ? AND kind = 'PENDING_RANGE' AND state = 'PROPOSED' AND plan_id IN "
            f"(SELECT id FROM {self.t('sw_plan')} WHERE user_id = ? AND as_of = ?) RETURNING id",
            (self.user_id, self.user_id, _bind(day)),
        ).fetchall()
        return len(rows)

    # -- the page's reads (this module's, not the contract's) ------------------------------

    def signals_for(self, day: dt.date) -> list[dict]:
        """Today's `sw_signal` rows, newest first, each with its plan line (if it became one)."""
        rows = self.conn.execute(
            f"SELECT s.id, s.instrument_id, i.symbol, s.setup, s.session_date, s.raised_at, "
            f"s.state, s.or_window_minutes, s.range_high, s.range_low, s.low_of_day, "
            f"s.last_price, s.entry, s.stop, s.plan_line_id, "
            f"(SELECT w.focus FROM {self.t('sw_watch')} w WHERE w.id = s.watch_id) AS focus "
            f"FROM {self.t('sw_signal')} s JOIN {self.t('instrument')} i ON i.id = s.instrument_id "
            "WHERE s.user_id = ? AND s.session_date = ? ORDER BY s.raised_at DESC, s.id DESC",
            (self.user_id, _bind(day)),
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            out.append(
                {
                    "id": int(r["id"]),
                    "instrument_id": int(r["instrument_id"]),
                    "symbol": str(r["symbol"]),
                    "setup": str(r["setup"]),
                    "session_date": _date(r["session_date"]),
                    "raised_at": _stamp(r["raised_at"]),
                    "state": str(r["state"]),
                    "or_window_minutes": (
                        int(r["or_window_minutes"]) if r["or_window_minutes"] is not None else None
                    ),
                    "range_high": _dec(r["range_high"], "range_high"),
                    "range_low": _dec(r["range_low"], "range_low"),
                    "low_of_day": _dec(r["low_of_day"], "low_of_day"),
                    "last_price": _dec(r["last_price"], "last_price"),
                    "entry": _dec(r["entry"], "entry"),
                    "stop": _dec(r["stop"], "stop"),
                    "plan_line_id": int(r["plan_line_id"]) if r["plan_line_id"] is not None else None,
                    # A14: in today's focus (top 5 flags by score + every EP) — shown on top.
                    "focus": bool(r["focus"]) if r["focus"] is not None else False,
                }
            )
        return out

    def signal_skip_for(self, instrument_id: int, day: dt.date) -> dict | None:
        """Why a trigger became no line: the latest SIGNAL plan's skip for this name today.

        The signal row links only to a line; a skipped trigger has a plan with a skip and no
        line, and the page owes the reader the reason rather than a blank cell.
        """
        row = self.conn.execute(
            f"SELECT k.reason, k.detail, p.plan_id FROM {self.t('sw_plan_skip')} k "
            f"JOIN {self.t('sw_plan')} p ON p.id = k.plan_id "
            "WHERE k.user_id = ? AND k.instrument_id = ? AND p.as_of = ? AND p.source = 'SIGNAL' "
            "ORDER BY p.built_at DESC, k.id DESC LIMIT 1",
            (self.user_id, int(instrument_id), _bind(day)),
        ).fetchone()
        if row is None:
            return None
        return {
            "reason": str(row["reason"]),
            "detail": str(row["detail"]) if row["detail"] is not None else None,
            "plan_id": str(row["plan_id"]),
        }

    def latest_plan(self, source: str) -> dict | None:
        row = self.conn.execute(
            f"SELECT id, plan_id, as_of, source, built_at, expires_at, gate, exposure_level, "
            f"total_risk_inr, total_new_exposure_inr FROM {self.t('sw_plan')} "
            "WHERE user_id = ? AND source = ? ORDER BY built_at DESC, id DESC LIMIT 1",
            (self.user_id, str(source)),
        ).fetchone()
        return self._plan_row(row) if row is not None else None

    def lines_for(self, plan_pk: int) -> list[dict]:
        rows = self.conn.execute(
            self._LINE_SELECT + self._line_from() + "WHERE l.user_id = ? AND l.plan_id = ? "
            "ORDER BY l.id",
            (self.user_id, int(plan_pk)),
        ).fetchall()
        return [self._line_row(r) for r in rows]

    def skips_for(self, plan_pk: int) -> list[dict]:
        rows = self.conn.execute(
            f"SELECT id, instrument_id, symbol, reason, detail FROM {self.t('sw_plan_skip')} "
            "WHERE user_id = ? AND plan_id = ? ORDER BY id",
            (self.user_id, int(plan_pk)),
        ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "instrument_id": int(r["instrument_id"]) if r["instrument_id"] is not None else None,
                "symbol": str(r["symbol"]),
                "reason": str(r["reason"]),
                "detail": str(r["detail"]) if r["detail"] is not None else None,
            }
            for r in rows
        ]

    def open_positions(self) -> list[dict]:
        """The book: every position with shares on it, unprotected ones first (`05` §2 gives
        the hub the same rule — a naked position sits in the same place whether or not there is
        one)."""
        rows = self.conn.execute(
            self._POSITION_SELECT
            + self._position_from()
            + "WHERE p.user_id = ? AND p.state IN ('OPEN', 'PARTIAL') AND p.quantity_open > 0 "
            "ORDER BY (p.gtt_id IS NULL) DESC, p.entry_date, p.id",
            (self.user_id,),
        ).fetchall()
        return [self._position_row(r) for r in rows]

    # --- SW15: "Scan now" ------------------------------------------------------------------

    def latest_scan_run(self) -> dict | None:
        """This user's newest `sw_scan_run` row, whatever its state — the status bar's line."""
        row = self.conn.execute(
            f"SELECT id, requested_at, started_at, finished_at, session_date, provisional, "
            f"status, detail, error, task_id FROM {self.t('sw_scan_run')} "
            f"WHERE user_id = ? ORDER BY requested_at DESC, id DESC LIMIT 1",
            (self.user_id,),
        ).fetchone()
        return self._scan_run_row(row) if row is not None else None

    def latest_market_scan(self) -> dict | None:
        """The newest `sw_market_daily` row's provenance: its date, whether its rows are
        provisional (a daytime scan over live-quote bars) and when they were scanned."""
        row = self.conn.execute(
            f"SELECT date, provisional, detail FROM {self.t('sw_market_daily')} "
            f"WHERE user_id = ? ORDER BY date DESC LIMIT 1",
            (self.user_id,),
        ).fetchone()
        if row is None:
            return None
        detail = _json_column(row["detail"])
        scan = detail.get("scan") if isinstance(detail, dict) else None
        scanned_at = _stamp(scan.get("scanned_at")) if isinstance(scan, dict) else None
        return {
            "date": _date(row["date"]),
            "provisional": bool(row["provisional"]),
            "scanned_at": scanned_at,
        }

    def request_scan(self, *, now: dt.datetime, min_interval: dt.timedelta,
                     stale_after: dt.timedelta, source: str = "desk") -> int:
        """Insert one `QUEUED` run for the worker's sweep to publish. Raises `ScanRefused`
        with the API's two statuses: 409 while one is in flight, 429 inside `min_interval`."""
        newest = self.latest_scan_run()
        if newest is not None:
            in_flight = newest["status"] in ("QUEUED", "RUNNING")
            if in_flight and newest["requested_at"] > now - stale_after:
                raise ScanRefused(
                    409, f"Scan {newest['id']} is {newest['status'].lower()}; its result is on "
                         f"its way.", run_id=newest["id"],
                )
            if newest["requested_at"] > now - min_interval:
                wait = min_interval - (now - newest["requested_at"])
                seconds = max(1, int(wait.total_seconds() + 0.999))
                raise ScanRefused(
                    429, f"A scan was requested {int((now - newest['requested_at']).total_seconds())} s "
                         f"ago; one a minute is the limit. Try again in {seconds} s.",
                    run_id=newest["id"], retry_after=seconds,
                )
        row = self.conn.execute(
            f"INSERT INTO {self.t('sw_scan_run')} (user_id, requested_at, status, detail) "
            f"VALUES (?, ?, 'QUEUED', ?) RETURNING id",
            (self.user_id, _bind(now), json.dumps({"source": source})),
        ).fetchone()
        return int(row["id"])

    def _scan_run_row(self, row: Any) -> dict:  # noqa: ANN401 - a driver row
        detail = _json_column(row["detail"])
        funnel = detail.get("funnel") if isinstance(detail, dict) else None
        return {
            "id": int(row["id"]),
            "requested_at": _stamp(row["requested_at"]),
            "started_at": _stamp(row["started_at"]),
            "finished_at": _stamp(row["finished_at"]),
            "session_date": _date(row["session_date"]),
            "provisional": bool(row["provisional"]),
            "status": str(row["status"]),
            "funnel": funnel if isinstance(funnel, dict) else None,
            "detail": detail if isinstance(detail, dict) else None,
            "error": str(row["error"]) if row["error"] is not None else None,
            "task_id": str(row["task_id"]) if row["task_id"] is not None else None,
        }

    def catalysts_for(self, instrument_ids: list[int]) -> dict[int, dict]:
        """SW11B (STANDING-ANSWERS A3): per name, the newest announcement's headline / stamp /
        link and the earnings date from `sw_catalyst` — what a trigger or a plan line links
        out to. Never the filing. Newest by stamp, undated last, on both sqlite and Postgres."""
        wanted = sorted({int(i) for i in instrument_ids})
        if not wanted:
            return {}
        marks = ", ".join("?" for _ in wanted)
        rows = self.conn.execute(
            f"SELECT instrument_id, headline, published_at, url, source, earnings_date "
            f"FROM {self.t('sw_catalyst')} WHERE user_id = ? AND instrument_id IN ({marks}) "
            "ORDER BY instrument_id, (published_at IS NULL), published_at DESC, id DESC",
            (self.user_id, *wanted),
        ).fetchall()
        out: dict[int, dict] = {}
        for r in rows:
            key = int(r["instrument_id"])
            view = out.setdefault(
                key, {"headline": None, "published_at": None, "url": None, "earnings_date": None}
            )
            if str(r["source"]) == "NSE_EVENT_CALENDAR":
                if view["earnings_date"] is None:
                    view["earnings_date"] = _date(r["earnings_date"])
            elif view["url"] is None:
                view["headline"] = str(r["headline"])
                view["published_at"] = _stamp(r["published_at"])
                view["url"] = str(r["url"])
        return out

    def recent_manage_actions(self, limit: int = MANAGE_ACTIONS_SHOWN) -> list[dict]:
        """The last `manage` actions the book saw: exit-kind plan lines, most recently touched
        first, with the state each reached. See `docs/swing/DECISIONS-SW.md` SW7.3 for why a
        manage action is read off `sw_plan_line` rather than a table of its own."""
        rows = self.conn.execute(
            self._LINE_SELECT + self._line_from()
            + "WHERE l.user_id = ? AND l.kind IN ('SELL_AT_OPEN', 'RAISE_GTT_STOP') "
            "ORDER BY l.updated_at DESC, l.id DESC LIMIT ?",
            (self.user_id, int(limit)),
        ).fetchall()
        return [self._line_row(r) for r in rows]


# --- the view ------------------------------------------------------------------------------


def _countdown(expires_at: dt.datetime | None, now: dt.datetime) -> str:
    """`m:ss` until the plan expires; `0:00` once it has. Never negative — an expired plan is
    shown, with its zero, so a person can see what they missed and why the button is gone."""
    if expires_at is None:
        return "0:00"
    left = int((expires_at - now).total_seconds())
    if left <= 0:
        return "0:00"
    return f"{left // 60}:{left % 60:02d}"


def _pct_of_sleeve(value: Decimal | None, capital: Decimal | None) -> Decimal | None:
    if value is None or not capital:
        return None
    return (value / capital * 100).quantize(_SCALE_2, rounding=decimal.ROUND_HALF_UP)


def _label(line: dict) -> str:
    """The line as a person reads it aloud — always led by SWING, because the weekly book's
    plan uses the same words for the same symbols and a second person at the desk must never
    mistake one for the other (`05` §3)."""
    kind = line["kind"]
    if kind == LineKind.BUY_ON_TRIGGER.value:
        return f"SWING BUY {line['symbol']} x{line['quantity']} @ {_price(line['trigger'])}"
    if kind == LineKind.SELL_AT_OPEN.value:
        return f"SWING SELL {line['symbol']} x{line['quantity']} at open"
    if kind == LineKind.RAISE_GTT_STOP.value:
        return f"SWING RAISE GTT {line['symbol']} to {_price(line['stop'])}"
    if kind == PENDING_KIND:
        return f"SWING PENDING {line['symbol']} — range at {_price(line['trigger'])}, no stop yet"
    return f"SWING {kind} {line['symbol']}"


def _price(value: Decimal | None) -> str:
    """A price at the schema's two places, whatever the driver stripped on the way back."""
    return "—" if value is None else f"{value:.2f}"


def _line_view(
    line: dict,
    plan: dict,
    *,
    now: dt.datetime,
    config: dict,
    held: Mapping[int, dict],
) -> dict:
    """A plan line with everything the row needs, including whether Confirm may appear.

    Confirm appears only on a PROPOSED line of an unexpired plan — and not on a BUY for a name
    the book already holds (the plan may have been built before the fill: a morning plan at
    09:10 and a SIGNAL fill at 09:31 for the same name), nor on an exit for a name it does not.
    `execute_line` re-checks all of this; the page simply does not offer what it would refuse.
    """
    expired = plan["expires_at"] is None or now > plan["expires_at"]
    position = held.get(line["instrument_id"])
    why_not = ""
    if line["kind"] not in EXECUTABLE_KINDS:
        # A7: information only. Never a button, whatever the state or the plan's clock.
        why_not = ("pending the opening range — not confirmable; the SIGNAL plan at window "
                   "close is the line" if line["state"] == PROPOSED
                   else f"{line['state'].lower()} (pending range)")
    elif line["state"] != PROPOSED:
        why_not = line["state"].lower()
    elif expired:
        why_not = "plan expired"
    elif line["kind"] == LineKind.BUY_ON_TRIGGER.value and position is not None:
        why_not = f"already held ({position['quantity_open']} open)"
    elif line["kind"] in EXIT_KINDS and position is None:
        why_not = "not a swing position"
    elif line["kind"] == LineKind.SELL_AT_OPEN.value and line["quantity"] > position["quantity_open"]:
        why_not = f"more than the {position['quantity_open']} open"
    elif (
        line["kind"] == LineKind.RAISE_GTT_STOP.value
        and line["stop"] is not None
        and position["stop"] is not None
        and line["stop"] <= position["stop"]
    ):
        why_not = f"not above the resting stop {position['stop']}"
    return {
        **line,
        "label": _label(line),
        "plan_expires_at": plan["expires_at"],
        "countdown": _countdown(plan["expires_at"], now),
        "expired": expired,
        "held": position is not None,
        "position": position,
        "pct_of_sleeve": _pct_of_sleeve(line["position_value"], config["sleeve_capital_inr"]),
        "cap_pct": config["max_position_pct"],
        "confirmable": why_not == "",
        "why_not": why_not,
    }


def _signal_text(signal: dict) -> str:
    window = signal["or_window_minutes"] or DEFAULT_SWING_CONFIG.opening_range.default_window_minutes
    state = signal["state"]
    if state == TriggerState.TRIGGERED.value:
        return f"{window}-min ORH {signal['range_high']} broken at {signal['last_price']}"
    if state == TriggerState.LOCKED_UPPER_CIRCUIT.value:
        return f"locked at the upper circuit ({signal['last_price']}) — not enterable"
    if state == TriggerState.BELOW_PIVOT.value:
        return f"{window}-min ORH {signal['range_high']} broken at {signal['last_price']}, below the pivot"
    return f"{state.lower().replace('_', ' ')} at {signal['last_price']}"


def _plan_view(
    plan: dict | None,
    store: PgSwingStore,
    *,
    now: dt.datetime,
    config: dict,
    held: Mapping[int, dict],
) -> dict | None:
    if plan is None:
        return None
    lines = [
        _line_view(line, plan, now=now, config=config, held=held)
        for line in store.lines_for(plan["id"])
    ]
    exits = sorted(
        (ln for ln in lines if ln["kind"] in EXIT_KINDS),
        key=lambda ln: (EXIT_KINDS.index(ln["kind"]), ln["symbol"]),
    )
    buys = [ln for ln in lines if ln["kind"] == LineKind.BUY_ON_TRIGGER.value]
    # A7: the live gaps holding a slot — shown with the buys, never with a button.
    pending = [ln for ln in lines if ln["kind"] == PENDING_KIND]
    expired = plan["expires_at"] is None or now > plan["expires_at"]
    return {
        **plan,
        "expired": expired,
        "countdown": _countdown(plan["expires_at"], now),
        "exits": exits,
        "buys": buys,
        "pending": pending,
        "skips": store.skips_for(plan["id"]),
        "line_count": len(lines),
    }


def monitor_state(
    *,
    enabled: bool,
    monitor_ran: bool,
    now: dt.datetime,
) -> dict:
    """idle / running / stopped / not enabled — and `did not run`, which `05` §3 does not list
    and which is the honest fifth answer once the window has closed with no mark in
    `sw_session` (SW11's `SWING_MONITOR_DID_NOT_START` alert is the same fact, out of hours).
    Derived, not observed: the desk cannot see the monitor process, only its flag, the clock,
    and the mark it leaves at close (DECISIONS-SW SW7.3)."""
    window = DEFAULT_SWING_CONFIG.opening_range
    opens = dt.time(*window.session_open)
    closes = dt.time(*window.monitor_close)
    opens_text = opens.strftime("%H:%M")
    closes_text = closes.strftime("%H:%M")
    if not enabled:
        return {"state": "not enabled", "label": "not enabled (BASKFY_SWING_MONITOR_ENABLED=false)"}
    if monitor_ran:
        return {"state": "stopped", "label": f"stopped at {closes_text}"}
    local = now.astimezone(IST)
    if local.weekday() >= 5:
        return {"state": "idle", "label": "idle (not a trading day)"}
    if local.time() < opens:
        return {"state": "idle", "label": f"idle — starts at {opens_text}"}
    if local.time() <= closes:
        return {"state": "running", "label": f"running since {opens_text}"}
    return {
        "state": "did not run",
        "label": f"did not run — no monitor_ran mark after {closes_text}",
    }


def token_age(now: dt.datetime, token_file: str | Path | None = None) -> dict:
    """How old the Kite access token is, read from the encrypted store without touching Kite.

    The file is checked for existence *before* the store is built: building it with no key in
    the environment writes a fallback key beside the token path (`app.token_store`), and a
    status bar must not create files as a side effect of being looked at.
    """
    path = Path(token_file or C.TOKEN_FILE)
    if not path.is_file():
        return {"present": False, "label": "no token — log in", "age_minutes": None,
                "expired": True}
    try:
        from .token_store import store_for  # noqa: PLC0415 - only once a token file exists

        token = store_for(path, getattr(C, "KITE_TOKEN_ENCRYPTION_KEY", "")).load()
    except Exception as exc:  # noqa: BLE001 - the bar reports, it does not fail the page
        log.warning("could not read the Kite token for the status bar: %s", exc)
        return {"present": False, "label": "token unreadable — log in", "age_minutes": None,
                "expired": True}
    age = now - token.issued_at.astimezone(IST)
    minutes = max(int(age.total_seconds() // 60), 0)
    hours, rest = divmod(minutes, 60)
    text = f"{hours}h {rest:02d}m old" if hours else f"{rest}m old"
    expired = token.is_expired(now=now)
    if expired:
        text = f"expired (issued {token.issued_at.astimezone(IST):%d %b %H:%M})"
    return {"present": True, "label": text, "age_minutes": minutes, "expired": expired}


def scan_status(market: dict | None, run: dict | None) -> dict:
    """SW15's status-bar line, from the newest market row and the newest run.

    `label` is the header's sentence — "provisional — scanned 13:42 IST from live quotes" for
    rows a daytime scan wrote, "re-scanned 18:02 IST from published bars" for an on-demand
    re-run, "" for a day the nightly wrote; `run_label` is the last run's state beside the
    button; `in_flight` disables it while a run is on its way.
    """
    label = ""
    provisional = bool(market and market["provisional"])
    stamp = market["scanned_at"] if market else None
    when = stamp.astimezone(IST).strftime("%H:%M IST") if stamp else None
    if provisional:
        label = f"provisional — scanned {when} from live quotes" if when else "provisional — from live quotes"
    elif when:
        label = f"re-scanned {when} from published bars"
    run_label = ""
    in_flight = False
    if run is not None:
        status = run["status"]
        in_flight = status in ("QUEUED", "RUNNING")
        if status == "QUEUED":
            run_label = "scan queued"
        elif status == "RUNNING":
            run_label = "scanning…"
        elif status == "FAILED":
            run_label = "last scan failed" + (f": {run['error']}" if run["error"] else "")
        else:
            done = run["finished_at"] or run["requested_at"]
            done_at = done.astimezone(IST).strftime("%H:%M IST") if done else "done"
            funnel = run["funnel"] or {}
            liquid = funnel.get("liquid")
            found = sum(int(n) for n in (funnel.get("candidates") or {}).values())
            counts = f" · {liquid} liquid, {found} flagged" if liquid is not None else ""
            what = "from live quotes" if run["provisional"] else "from published bars"
            run_label = f"last scan {done_at} {what}{counts}"
    return {
        "date": market["date"] if market else None,
        "provisional": provisional,
        "scanned_at": stamp,
        "label": label,
        "run": run,
        "run_label": run_label,
        "in_flight": in_flight,
    }


def _refresh(now: dt.datetime) -> tuple[int, int | None]:
    """(poll interval in ms, ms until the window opens today) — 5 s inside 09:15–10:45, nothing
    outside it, and a one-shot timer if the page was opened before the window."""
    window = DEFAULT_SWING_CONFIG.opening_range
    local = now.astimezone(IST)
    opens = local.replace(hour=window.session_open[0], minute=window.session_open[1],
                          second=0, microsecond=0)
    closes = local.replace(hour=window.monitor_close[0], minute=window.monitor_close[1],
                           second=0, microsecond=0)
    if opens <= local <= closes:
        return POLL_MS, None
    if local < opens:
        return 0, int((opens - local).total_seconds() * 1000)
    return 0, None


def _fingerprint(signals: list[dict], plans: list[dict | None], positions: list[dict],
                 session: dict | None, scan: dict | None = None) -> str:
    """Changes when something a person would want to see changed: a new signal, a line that
    moved state, a position armed or closed, a counter, a scan that finished (SW15). The page
    reloads on a change and only on a change, so an inline result survives an idle poll."""
    parts: list[str] = []
    if scan and scan.get("run"):
        parts.append(f"c{scan['run']['id']}:{scan['run']['status']}")
    for s in signals:
        line_state = s["line"]["state"] if s.get("line") else None
        parts.append(f"s{s['id']}:{s['state']}:{s['plan_line_id']}:{line_state}")
    for p in plans:
        if p is None:
            continue
        parts.append(f"p{p['id']}:{p['expired']}")
        for ln in p["exits"] + p["buys"] + p.get("pending", []):
            parts.append(f"l{ln['id']}:{ln['state']}")
    for pos in positions:
        parts.append(f"o{pos['id']}:{pos['state']}:{pos['gtt_id']}:{pos['quantity_open']}")
    if session:
        parts.append(
            f"n{session['signals']}/{session['confirms']}/{session['fills']}/"
            f"{session['manage_actions']}/{session['monitor_ran']}"
        )
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def build_view(
    store: PgSwingStore,
    *,
    now: dt.datetime,
    dry_run: bool | None = None,
    execution_enabled: bool | None = None,
    monitor_enabled: bool | None = None,
    token: dict | None = None,
) -> dict:
    """The dict `swing.html` renders — and `/swing/data` returns — per `05` §3.

    Flags default to the process's (`app.config`), the token to the encrypted store's; a test
    passes its own so the page can be rendered in every state without an environment.
    """
    if now.tzinfo is None:
        raise ValueError("build_view needs a tz-aware `now`; the desk passes datetime.now(IST)")
    now = now.astimezone(IST)
    today = now.date()
    dry_run = C.DRY_RUN if dry_run is None else dry_run
    execution_enabled = C.SWING_EXECUTION_ENABLED if execution_enabled is None else execution_enabled
    monitor_enabled = C.SWING_MONITOR_ENABLED if monitor_enabled is None else monitor_enabled

    config = store.config()
    positions = store.open_positions()
    held = {p["instrument_id"]: p for p in positions}
    session = store.session(today)

    # --- triggers ------------------------------------------------------------------------
    triggers: list[dict] = []
    for signal in store.signals_for(today):
        line = store.line(signal["plan_line_id"]) if signal["plan_line_id"] else None
        plan = store.plan(line["plan_id"]) if line else None
        line_view = (
            _line_view(line, plan, now=now, config=config, held=held) if line and plan else None
        )
        skip = None
        if line_view is None and signal["state"] == TriggerState.TRIGGERED.value:
            skip = store.signal_skip_for(signal["instrument_id"], today)
        locked = signal["state"] == TriggerState.LOCKED_UPPER_CIRCUIT.value
        triggers.append(
            {
                **signal,
                "text": _signal_text(signal),
                "locked": locked,
                "line": line_view,
                "plan": plan,
                "skip": skip,
                "confirmable": bool(line_view and line_view["confirmable"]),
            }
        )
    # A14: the focus names first (a stable sort keeps newest-first inside each group).
    triggers.sort(key=lambda t: 0 if t.get("focus") else 1)

    # --- the plan ------------------------------------------------------------------------
    morning = store.latest_plan("MORNING")
    if morning is not None and morning["as_of"] != today:
        morning = None  # yesterday's morning plan is history, not the plan
    morning_view = _plan_view(morning, store, now=now, config=config, held=held)
    preview_view = _plan_view(store.latest_plan("EOD_PREVIEW"), store, now=now, config=config,
                              held=held)

    # --- SW11B: the catalyst link on triggers and plan lines (A3) -------------------------
    # One read for every name on the page; each row gets `catalyst` (or None) and the template
    # renders a link that opens the exchange's copy, never the text.
    linked_lines = [
        ln
        for plan_view in (morning_view, preview_view)
        if plan_view is not None
        for ln in (*plan_view["exits"], *plan_view["buys"], *plan_view["pending"])
    ]
    catalysts = store.catalysts_for(
        [t["instrument_id"] for t in triggers] + [ln["instrument_id"] for ln in linked_lines]
    )
    for t in triggers:
        t["catalyst"] = catalysts.get(t["instrument_id"])
    for ln in linked_lines:
        ln["catalyst"] = catalysts.get(ln["instrument_id"])

    # --- the book ------------------------------------------------------------------------
    book = []
    for pos in positions:
        book.append(
            {
                **pos,
                "naked": pos["gtt_id"] is None,
                "value_inr": (pos["entry_avg"] * pos["quantity_open"]).quantize(_SCALE_2)
                if pos["entry_avg"] is not None else None,
            }
        )
    manage = [
        {**ln, "label": _label(ln)} for ln in store.recent_manage_actions(MANAGE_ACTIONS_SHOWN)
    ]

    # --- the status bar --------------------------------------------------------------------
    counters = session or {
        "session_date": today, "mode": None, "monitor_ran": False, "signals": 0,
        "confirms": 0, "fills": 0, "manage_actions": 0, "notes": None,
    }
    simulated = dry_run or not execution_enabled
    # A9: the plan header's line — half risk while the countdown runs and a confirm is real.
    left = int(config["first_live_sessions_left"])
    multiplier = first_live_multiplier(
        sessions_left=left, execution_enabled=not simulated, config=DEFAULT_SWING_CONFIG.sizing
    )
    risk_in_force = (Decimal(str(config["risk_per_trade_pct"])) * multiplier).quantize(
        Decimal("0.001")
    )
    first_live_header = (
        f"first live sessions: {left} left · risk {risk_in_force}%" if left > 0 else ""
    )
    # A8: live LIMIT buys accepted and not yet complete — the reconcile / cutoff buttons' cue.
    sent_lines = store.sent_buy_lines(today)
    poll_ms, window_opens_in_ms = _refresh(now)
    plans_for_print = [morning_view, preview_view]
    scan = scan_status(store.latest_market_scan(), store.latest_scan_run())
    return {
        "available": True,
        "reason": "",
        "now": now,
        "today": today,
        "status": {
            "dry_run": dry_run,
            "execution_enabled": execution_enabled,
            "monitor_enabled": monitor_enabled,
            "simulated": simulated,
            "mode_label": (
                "SIMULATED — every confirm journals simulated=true"
                + (" (DRY_RUN=true)" if dry_run else " (BASKFY_SWING_EXECUTION_ENABLED=false)")
                if simulated
                else "LIVE — confirms place real orders"
            ),
            "monitor": monitor_state(
                enabled=monitor_enabled, monitor_ran=counters["monitor_ran"], now=now
            ),
            "token": token if token is not None else token_age(now),
            "session": {**counters, "recorded": session is not None},
            "scan": scan,
        },
        "sleeve": {
            "capital_inr": config["sleeve_capital_inr"],
            "risk_per_trade_pct": config["risk_per_trade_pct"],
            "max_position_pct": config["max_position_pct"],
            "max_open_positions": config["max_open_positions"],
            "exposure_level": config["exposure_level"],
            "first_live_sessions_left": config["first_live_sessions_left"],
            "risk_multiplier": str(multiplier),
            "risk_pct_in_force": risk_in_force,
            "first_live_header": first_live_header,
            "half_risk": multiplier < 1,
            "configured": config.get("present", True),
        },
        "sent_lines": [{"id": ln["id"], "symbol": ln["symbol"], "quantity": ln["quantity"],
                        "journal_ref": ln["journal_ref"], "position_id": ln["position_id"]}
                       for ln in sent_lines],
        "triggers": triggers,
        "plans": {"morning": morning_view, "preview": preview_view},
        "book": {
            "positions": book,
            "naked_count": sum(1 for p in book if p["naked"]),
            "manage": manage,
        },
        "poll_ms": poll_ms,
        "window_opens_in_ms": window_opens_in_ms,
        "fingerprint": _fingerprint(triggers, plans_for_print, positions, session, scan),
    }


def unavailable_view(reason: str, *, now: dt.datetime) -> dict:
    """What the page shows when the store cannot be read — the desk on sqlite has no `sw_`
    tables at all, and a Postgres that is down is a Postgres that is down."""
    return {
        "available": False,
        "reason": reason,
        "now": now.astimezone(IST),
        "today": now.astimezone(IST).date(),
        "status": {
            "dry_run": C.DRY_RUN,
            "execution_enabled": C.SWING_EXECUTION_ENABLED,
            "monitor_enabled": C.SWING_MONITOR_ENABLED,
            "simulated": C.DRY_RUN or not C.SWING_EXECUTION_ENABLED,
            "mode_label": "",
            "monitor": monitor_state(enabled=C.SWING_MONITOR_ENABLED, monitor_ran=False, now=now),
            "token": token_age(now),
            "session": {"recorded": False, "signals": 0, "confirms": 0, "fills": 0,
                        "manage_actions": 0, "monitor_ran": False, "mode": None},
            "scan": scan_status(None, None),
        },
        "sleeve": None,
        "triggers": [],
        "plans": {"morning": None, "preview": None},
        "book": {"positions": [], "naked_count": 0, "manage": []},
        "poll_ms": 0,
        "window_opens_in_ms": None,
        "fingerprint": "",
    }


# --- wiring: the connection, the gateway, the templates -----------------------------------


@contextlib.contextmanager
def open_store() -> Iterator[PgSwingStore]:
    """The sole user's store over the desk's connection. Postgres → `public.sw_*`; a sqlite
    desk has no swing tables and the page says so rather than pretending. Tests replace this
    with a store over their own sqlite file."""
    from .analytics import db as _db  # noqa: PLC0415 - the desk imports its DB lazily everywhere

    with _db.connect() as conn:
        schema = SCHEMA if _db.DB_BACKEND == "postgres" else ""
        yield PgSwingStore(
            conn,
            user_id=C.SOLE_USER_ID,
            schema=schema,
            broker_account_id=C.SOLE_BROKER_ACCOUNT_ID,
        )


_swing_gateway: Any = None


def swing_gateway() -> Any:  # noqa: ANN401 - an OrderGateway, built by the execute module
    """The swing book's gateway, built once and lazily by `app.swing_execute` — never at
    import, because building it needs a Kite client and a page must import without one. It
    shares `app.main`'s risk manager: one account, one daily-loss cap, one order counter, so
    the swing book cannot spend a limit the weekly book has already used."""
    global _swing_gateway
    if _swing_gateway is None:
        from . import main as _main  # noqa: PLC0415 - main mounts this router; import at call
        from . import swing_execute  # noqa: PLC0415 - the sibling module, at call time

        _main.gateway()  # builds `_risk` as a side effect
        _swing_gateway = swing_execute.build_swing_gateway(_main.kite().kc, _main._risk)
    return _swing_gateway


def last_price(symbol: str) -> Decimal | None:
    """The instrument's last traded price from the broker, or None when there is no session.

    A SELL's simulated fill and a RAISE's "is the stop below the market" check need one, and
    `execute_line` refuses — never guesses — without it; a re-arm needs one above the stop.
    A read, not an order: it goes through `Kite.ltp`, and a desk with no token gets None and
    a BLOCKED outcome that says so, rather than a fill at a made-up price.
    """
    try:
        from . import main as _main  # noqa: PLC0415 - main mounts this router; import at call

        value = _main.kite().ltp([symbol]).get(symbol)
    except Exception as exc:  # noqa: BLE001 - no session, no price; the outcome says so
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
        log.warning("swing view unavailable: %s", exc)
        return unavailable_view(str(exc)[:200], now=now)


# --- routes ---------------------------------------------------------------------------------


@router.get("/swing", response_class=HTMLResponse)
def swing_page(request: Request):
    return _templates().TemplateResponse(request, "swing.html", {"v": _view()})


@router.get("/swing/data")
def swing_data():
    return _jsonable(_view())


def _outcome_json(outcome: Any, **extra: Any) -> dict:  # noqa: ANN401 - an ExecOutcome
    body = dataclasses.asdict(outcome) if dataclasses.is_dataclass(outcome) else dict(vars(outcome))
    return _jsonable({**body, **extra})


def _refused_json(exc: Exception, **extra: Any) -> dict:
    """A guard's refusal as the outcome vocabulary, the way `/execute` reports one per order:
    BLOCKED with the guard's words. `execute_line` has already marked the line REJECTED; the
    row must show why rather than a 500 that says nothing."""
    from . import swing_execute  # noqa: PLC0415

    return _jsonable({"status": "BLOCKED", "reason": str(exc), "order": None, "gtt": None,
                      "position_id": None, "simulated": swing_execute.swing_gates().dry_run,
                      **extra})


@router.post("/swing/execute")
async def swing_execute_line(
    plan_id: str = Form(...), line_id: int = Form(...), confirm: str = Form(...)
):
    """One line, one click. The gates (confirm, the plan, its expiry, the line's state) are
    `execute_line`'s and raise through here as 400 / 404 / 410 / 409; the outcome comes back as
    JSON for the row to render inline. There is no route that takes more than one line."""
    from . import swing_execute  # noqa: PLC0415 - the sibling module, resolved at call time

    with open_store() as store:
        line = store.line(line_id)
        if line is not None and line["kind"] not in EXECUTABLE_KINDS:
            # A7: a PENDING_RANGE line is information — the route refuses it before the
            # execute module is even asked (which would refuse it again).
            raise HTTPException(400, f"A {line['kind']} line cannot be executed — it is "
                                     f"information only; the SIGNAL plan at range close is "
                                     f"the line.")
        # Only an exit needs the market: a buy's entry is its trigger. Looked up before the
        # call so a refusal (400/404/410/409) costs no broker read.
        price = (
            last_price(line["symbol"]) if line is not None and line["kind"] in EXIT_KINDS
            else None
        )
        try:
            outcome = await swing_execute.execute_line(
                store, swing_gateway(), plan_id=plan_id, line_id=line_id, confirm=confirm,
                now=_now(), last_price=price, orders=order_source(),
            )
        except UntouchableInstrumentError as exc:
            return _refused_json(exc, line_id=line_id)
    return _outcome_json(outcome, line_id=line_id)


class KiteOrders:
    """A8's `OrderSource` over the desk's Kite session — a READ of the order history, the
    last row of which is the order's current state. No session → an OPEN, unfilled report,
    so the confirm answers `SENT` and the reconcile finds it later."""

    def __init__(self, kite: Any) -> None:  # noqa: ANN401 - the desk's Kite wrapper
        self.kite = kite

    def order_status(self, order_id: str) -> Any:  # noqa: ANN401 - an OrderReport
        from . import swing_execute  # noqa: PLC0415

        try:
            history = self.kite.kc.order_history(order_id)
        except Exception as exc:  # noqa: BLE001 - no session, no answer; the line stays SENT
            log.warning("no order history for %s: %s", order_id, exc)
            return swing_execute.OrderReport("OPEN", 0, Decimal(0))
        last = history[-1] if history else {}
        return swing_execute.OrderReport(
            str(last.get("status") or "OPEN"),
            int(last.get("filled_quantity") or 0),
            Decimal(str(last.get("average_price") or 0)),
        )


def order_source() -> Any:  # noqa: ANN401 - an OrderSource, or None without a session
    """The broker's order book for the fill poll — None when the desk has no Kite session (a
    sqlite desk, a test), in which case a live confirm answers `SENT` without polling."""
    try:
        from . import main as _main  # noqa: PLC0415

        return KiteOrders(_main.kite())
    except Exception as exc:  # noqa: BLE001 - reported, not fatal: the poll is a convenience
        log.warning("no order source: %s", exc)
        return None


@router.post("/swing/scan")
def swing_scan_now():
    """SW15: queue a detection scan — one `sw_scan_run` row the worker's sweep publishes. Not an
    order and not a confirm: the worker reads quotes and writes detection rows, labelled
    provisional during the session. 409 while one is in flight, 429 inside a minute."""
    now = _now()
    with open_store() as store:
        try:
            run_id = store.request_scan(
                now=now,
                min_interval=dt.timedelta(seconds=C.SWING_SCAN_MIN_INTERVAL_SECONDS),
                stale_after=dt.timedelta(seconds=C.SWING_SCAN_STALE_AFTER_SECONDS),
            )
        except ScanRefused as exc:
            headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
            raise HTTPException(exc.status, exc.reason, headers=headers) from exc
    return _jsonable({"run_id": run_id, "status": "QUEUED", "requested_at": now})


@router.post("/swing/reconcile")
async def swing_reconcile(confirm: str = Form(...)):
    """A8: ask the broker about every SENT buy of today and apply what has filled through the
    postback handler — the pull the page offers while a live LIMIT is resting. A non-money
    write: it grows a position the broker already filled and re-sizes its stop; it never
    places a buy."""
    from . import swing_execute  # noqa: PLC0415

    if confirm != "true":
        raise HTTPException(400, "Reconciling fills requires explicit confirmation.")
    source = order_source()
    applied: list[dict] = []
    with open_store() as store:
        for line in store.sent_buy_lines(_now().astimezone(IST).date()):
            if source is None or not line["journal_ref"]:
                continue
            seen = source.order_status(line["journal_ref"])
            outcome = await swing_execute.on_order_update(
                store, swing_gateway(),
                {"order_id": line["journal_ref"], "status": seen.status,
                 "filled_quantity": seen.filled_quantity, "average_price": seen.average_price},
                now=_now(),
            )
            if outcome is not None:
                applied.append(_outcome_json(outcome, line_id=line["id"]))
    return _jsonable({"reconciled": applied, "count": len(applied)})


@router.post("/swing/cutoff")
async def swing_cutoff(confirm: str = Form(...)):
    """The 10:45 sweep (A8, A7): cancel every open remainder through the gateway, free every
    pending-range slot nothing claimed. Posted by the page's button, or by hand; the monitor
    process runs the same chore itself at 10:45 (`app.swing_clock`, SW11), and
    `SWING_ORDER_OPEN_AFTER_CUTOFF` says when neither did."""
    from . import swing_execute  # noqa: PLC0415

    if confirm != "true":
        raise HTTPException(400, "The 10:45 sweep requires explicit confirmation.")
    with open_store() as store:
        report = await swing_execute.cutoff_open_orders(
            store, swing_gateway(), orders=order_source(), now=_now()
        )
    return _jsonable({
        "reconciled": report.reconciled, "cancelled": report.cancelled,
        "cancel_failed": report.cancel_failed, "slots_freed": report.slots_freed,
        "outcomes": [_outcome_json(o) for o in report.outcomes],
    })


@router.post("/swing/rearm")
async def swing_rearm(position_id: int = Form(...), confirm: str = Form(...)):
    """Re-arm the GTT of a naked position — the only other order-shaped action on the page,
    and it is a stop, not a buy."""
    from . import swing_execute  # noqa: PLC0415

    with open_store() as store:
        pos = store.position(position_id)
        price = last_price(pos["symbol"]) if pos is not None else None
        try:
            outcome = await swing_execute.rearm_gtt(
                store, swing_gateway(), position_id=position_id, confirm=confirm, now=_now(),
                last_price=price,
            )
        except UntouchableInstrumentError as exc:
            return _refused_json(exc, position_id=position_id)
    return _outcome_json(outcome, position_id=position_id)
