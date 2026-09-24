"""The FO book's orders, on paper: the confirm FO8 will call, and the exits and rolls the monitor
raises (``docs/fno/04`` §1-§3, §10; ``06`` FO7/FO8).

Law 2: ``packages/execution`` is the only path to an order. Every leg here is an
``OrderGateway.place(..., product="NRML", exchange="NFO", fo_plan=FoPlanRef(...))`` and every F2
stop an ``OrderGateway.place_gtt_stop``/``modify_gtt_quantity`` with the same reference, on the FO
gateway (:func:`fo_gateway`), which is wired with ``baskfy_core.fno.covered.uncovered`` as its
covered-overnight rule (FO6.1). An option order's reference carries the order as its ``step``,
the plan's filled legs, and the broker's NRML option book **net of the plan's own fills**
(``covered.net_of_plan``, FO6.3), so the gateway re-proves every prefix before the order.

**Paper only, and pinned so** (DECISIONS-FO FO7.1). The FO gateway's switches are the sleeve's
``fno_gates.product_gates`` with ``dry_run`` forced true: in PAPER that is what they are anyway
(FO6.5), and a LIVE sleeve — all four of Maulik's switches — is refused before any order
(``LIVE_NOT_BUILT``), because a live fill must be read back from the broker's order book, which
FO7 does not build. Every leg therefore runs the real guards, the covered-overnight guard, the risk
check, the rate limit and the journal, comes back ``DRY_RUN`` without a broker call, and is filled
by walking the leg's live depth (``baskfy_core.options.execution.simulate_fill``, the options
pack's simulator) into ``fo_fill`` with ``simulated=true``.

**Sequences.** An F1 entry is ``baskfy_core.options.executor.run_entry`` (longs first; a leg that
does not fill in full abandons the entry before any short, and the filled longs are closed at once:
``ABANDONED_PARTIAL``); an exit is ``run_exit`` (shorts first, a protecting long never before its
short). F2 is one leg: bought with two limits, sold with a third, marketable attempt (a sale
reduces risk), its GTT placed in the same session.

**Nothing here is scheduled and nothing here confirms.** :func:`execute_entry` runs only when
called with ``confirm=True`` and an ``ISSUED`` plan inside its 30 minutes — FO8's
``POST /fno/execute`` is its one caller. :func:`run_pending` sends the EXIT and ROLL plans the
monitor raised, which are the entry's confirm at work ("exits under a confirm are not entries",
``02`` Track B; options PACK.2).
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from baskfy_core.fno.condor import ENTRY_SEQUENCE
from baskfy_core.fno.config import FoSleeve, PlanKind, PlanState, Structure
from baskfy_core.fno.covered import OptionPosition, net_of_plan, uncovered
from baskfy_core.options.chain import Level
from baskfy_core.options.config import ExecutionConfig, Mode, OptionType, Side, Sleeve
from baskfy_core.options.execution import Attempt, LegRole, next_attempt, simulate_fill
from baskfy_core.options.executor import Book, Fill, Outcome, run_entry, run_exit
from baskfy_execution.gtt import GTT_MODIFIED_STATUSES, GTT_PLACED_STATUSES
from baskfy_execution.guards import FoPlanRef

log = logging.getLogger("desk.fno_execute")

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SCHEMA = "public"
TICK = Decimal("0.05")
DRY_RUN = "DRY_RUN"
FUTURE_ROLE = "LONG_FUTURE"
FUTURE_TYPE = "XX"
#: The options executor's rules for a leg are sleeve-blind except "which close reduces risk":
#: for a condor only the short buy-backs do, which is O1's reading — so F1 runs as O1 there.
_CONDOR_RULES = Sleeve.O1M
_CENT = Decimal("0.01")
_DUPLICATE = "DUPLICATE"


class Refused(Exception):
    """A confirm or an action the desk answers with a status and a reason, before any order."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# --- rows ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FoLegRow:
    id: int
    entry_seq: int
    role: str
    tradingsymbol: str
    instrument_token: int
    expiry: dt.date
    strike: Decimal | None
    option_type: str
    side: str
    quantity: int


@dataclass(frozen=True)
class FoPlanRow:
    pk: int
    plan_id: str
    sleeve: FoSleeve
    symbol: str
    trade_date: dt.date
    structure: str
    kind: str
    status: str
    issued_at: dt.datetime
    expires_at: dt.datetime
    lots: int
    lot_size: int
    parent_plan_id: str | None
    hard_exit_date: dt.date | None
    confirmed_at: dt.datetime | None
    detail: dict[str, Any]
    legs: tuple[FoLegRow, ...]


@dataclass(frozen=True)
class PositionRow:
    id: int
    sleeve: FoSleeve
    symbol: str
    structure: str
    entry_plan_id: str
    legs: dict[str, Any]
    lots: int
    lot_size: int
    entry_price: Decimal | None
    entry_credit: Decimal | None
    max_loss_inr: Decimal | None
    stop_price: Decimal | None
    gtt_id: str | None
    hard_exit_date: dt.date | None
    next_roll_date: dt.date | None
    opened_at: dt.datetime
    simulated: bool

    @property
    def leg_list(self) -> list[dict[str, Any]]:
        raw = self.legs.get("legs")
        return [x for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []

    @property
    def carry(self) -> dict[str, Any]:
        raw = self.legs.get("carry")
        return dict(raw) if isinstance(raw, dict) else {}


@dataclass(frozen=True)
class FoQuote:
    """A contract's live book: the touch, the depth the simulator walks, last price and OI."""

    bid: Decimal | None
    ask: Decimal | None
    bids: tuple[Level, ...] = ()
    asks: tuple[Level, ...] = ()
    last: Decimal | None = None
    oi: int | None = None

    @property
    def two_sided(self) -> bool:
        return self.bid is not None and self.ask is not None and self.bid > 0 and self.ask > 0

    @property
    def mid(self) -> Decimal | None:
        if not self.two_sided or self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2


#: The FO ``OrderGateway`` (``app.core.gateway``'s shim over ``baskfy_execution``): only its
#: ``place``, ``place_gtt_stop``, ``modify_gtt_quantity`` and ``delete_gtt`` are called.
Gateway = Any

#: Tradingsymbols in, their books out. A symbol Kite does not answer is absent.
QuoteSource = Callable[[Sequence[str]], Mapping[str, FoQuote]]
#: The broker's NRML option book (MIS excluded) **net of the plan's filled legs**, which it is
#: handed (FO1.2/FO6.3): a live reader is ``lambda filled: net_of_plan(raw(), filled)``.
BrokerBook = Callable[[tuple[OptionPosition, ...]], tuple[OptionPosition, ...]]


@dataclass
class FoOutcome:
    plan_id: str
    sleeve: str
    kind: str
    outcome: str
    simulated: bool
    orders: int
    position_id: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def paper_book(plan_filled: tuple[OptionPosition, ...]) -> tuple[OptionPosition, ...]:
    """In PAPER the broker holds none of the plan's legs — so there is nothing to net them out of
    (``net_of_plan`` over an empty book would count each fill as a short) — and the real
    account's NRML book is not cover for a simulated short (FO7.4): the plan covers itself."""
    del plan_filled
    return ()


def live_book(raw: Callable[[], tuple[OptionPosition, ...]]) -> BrokerBook:
    """A broker reader for a live plan: the raw NRML book net of the plan's own fills (FO6.3)."""
    return lambda plan_filled: tuple(net_of_plan(raw(), plan_filled))


# --- small conversions ---------------------------------------------------------------------------


def _t(schema: str, table: str) -> str:
    return f"{schema}.{table}" if schema else table


def _aware(at: dt.datetime | str) -> dt.datetime:
    value = dt.datetime.fromisoformat(at) if isinstance(at, str) else at
    return value if value.tzinfo is not None else value.replace(tzinfo=IST)


def _day(value: dt.date | str) -> dt.date:
    return dt.date.fromisoformat(value[:10]) if isinstance(value, str) else value


def _opt_day(value: object) -> dt.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def _dec(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _px(value: object) -> Decimal | None:
    """A ``numeric(18,2)`` / ``(12,2)`` column read back at its storage precision (house rule 8),
    whatever type the adapter hands over."""
    found = _dec(value)
    return None if found is None else found.quantize(Decimal("0.01"))


def _json(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}
    return {}


def money(value: Decimal) -> Decimal:
    return value.quantize(_CENT)


def _jsonable(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def dumps(value: object) -> str:
    return json.dumps(_jsonable(value))


# --- the store on the fo_ tables -----------------------------------------------------------------


class FoStore:
    """The FO book over the screener's ``fo_`` tables through the desk's Postgres adapter (the way
    ``PgOptionsStore`` reaches ``op_``). Every statement names its schema and its user."""

    def __init__(self, conn: Any, *, user_id: int, schema: str = SCHEMA) -> None:  # noqa: ANN401
        self.conn = conn
        self.user_id = user_id
        self.schema = schema

    def t(self, table: str) -> str:
        return _t(self.schema, table)

    # plans ------------------------------------------------------------------------------------

    def plan(self, plan_id: str) -> FoPlanRow | None:
        row = self.conn.execute(
            f"SELECT * FROM {self.t('fo_plan')} WHERE user_id = ? AND plan_id = ?",
            (self.user_id, plan_id),
        ).fetchone()
        return None if row is None else self._plan(row)

    def _plan(self, row: Any) -> FoPlanRow:  # noqa: ANN401 - a row mapping
        legs = self.conn.execute(
            "SELECT id, entry_seq, role, tradingsymbol, instrument_token, expiry, strike, "
            f"option_type, side, quantity FROM {self.t('fo_leg')} WHERE plan_id = ? "
            "ORDER BY entry_seq",
            (row["id"],),
        ).fetchall()
        return FoPlanRow(
            pk=int(row["id"]), plan_id=str(row["plan_id"]), sleeve=FoSleeve(str(row["sleeve"])),
            symbol=str(row["symbol"]), trade_date=_day(row["trade_date"]),
            structure=str(row["structure"]), kind=str(row["kind"]), status=str(row["status"]),
            issued_at=_aware(row["issued_at"]), expires_at=_aware(row["expires_at"]),
            lots=int(row["lots"]), lot_size=int(row["lot_size"]),
            parent_plan_id=row["parent_plan_id"], hard_exit_date=_opt_day(row["hard_exit_date"]),
            confirmed_at=None if row["confirmed_at"] is None else _aware(row["confirmed_at"]),
            detail=_json(row["detail"]),
            legs=tuple(
                FoLegRow(int(lg["id"]), int(lg["entry_seq"]), str(lg["role"]),
                         str(lg["tradingsymbol"]), int(lg["instrument_token"]), _day(lg["expiry"]),
                         _px(lg["strike"]), str(lg["option_type"]), str(lg["side"]),
                         int(lg["quantity"]))
                for lg in legs
            ),
        )  # fmt: skip

    def insert_plan(self, head: Mapping[str, Any], legs: Sequence[Mapping[str, Any]]) -> bool:
        """One plan and its legs; ``False`` (and nothing written) when the plan id exists — the
        id is deterministic, so a second raise is a no-op (house rule 7)."""
        cols = ["user_id", *head.keys()]
        values = [self.user_id, *(dumps(v) if k == "detail" else v for k, v in head.items())]
        row = self.conn.execute(
            f"INSERT INTO {self.t('fo_plan')} ({', '.join(cols)}) VALUES "
            f"({', '.join('?' for _ in cols)}) ON CONFLICT (plan_id) DO NOTHING RETURNING id",
            tuple(values),
        ).fetchone()
        if row is None:
            return False
        for leg in legs:
            lcols = ["user_id", "plan_id", *leg.keys()]
            self.conn.execute(
                f"INSERT INTO {self.t('fo_leg')} ({', '.join(lcols)}) VALUES "
                f"({', '.join('?' for _ in lcols)})",
                (self.user_id, int(row["id"]), *leg.values()),
            )
        return True

    def set_status(self, plan_id: str, status: str, reason: str | None = None,
                   *, only_from: Sequence[str] | None = None) -> None:  # fmt: skip
        guard = "" if not only_from else f" AND status IN ({', '.join('?' for _ in only_from)})"
        self.conn.execute(
            f"UPDATE {self.t('fo_plan')} SET status = ?, reason = COALESCE(?, reason) "
            f"WHERE user_id = ? AND plan_id = ?{guard}",
            (status, reason, self.user_id, plan_id, *(only_from or ())),
        )

    def confirm(self, plan: FoPlanRow, at: dt.datetime) -> bool:
        row = self.conn.execute(
            f"UPDATE {self.t('fo_plan')} SET status = 'CONFIRMED', confirmed_at = ? "
            "WHERE id = ? AND status = 'ISSUED' RETURNING id",
            (_aware(at), plan.pk),
        ).fetchone()
        return row is not None

    def merge_detail(self, plan_id: str, extra: Mapping[str, Any]) -> None:
        self.conn.execute(
            f"UPDATE {self.t('fo_plan')} SET detail = COALESCE(detail, '{{}}'::jsonb) || ?::jsonb "
            "WHERE user_id = ? AND plan_id = ?",
            (dumps(dict(extra)), self.user_id, plan_id),
        )

    def add_violation(self, plan_id: str, code: str, message: str, at: dt.datetime) -> None:
        """A paper-checklist violation (``04`` §9) on the entry plan, append-only."""
        item = dumps([{"code": code, "message": message, "at": _aware(at)}])
        self.conn.execute(
            f"UPDATE {self.t('fo_plan')} SET detail = COALESCE(detail, '{{}}'::jsonb) || "
            "jsonb_build_object('violations', COALESCE(detail->'violations', '[]'::jsonb) || "
            "?::jsonb) WHERE user_id = ? AND plan_id = ?",
            (item, self.user_id, plan_id),
        )

    def next_attempt(self, plan_pk: int, key: str) -> int:
        """The attempt number for ``key`` (a symbol and direction), persisted on the plan so a
        client id is never reused across passes or restarts (the gateway would answer it
        ``DUPLICATE``)."""
        row = self.conn.execute(
            f"UPDATE {self.t('fo_plan')} SET detail = COALESCE(detail, '{{}}'::jsonb) || "
            "jsonb_build_object('attempts', COALESCE(detail->'attempts', '{}'::jsonb) || "
            "jsonb_build_object(?::text, COALESCE((detail->'attempts'->>?)::int, 0) + 1)) "
            "WHERE id = ? RETURNING (detail->'attempts'->>?)::int AS n",
            (key, key, plan_pk, key),
        ).fetchone()
        return int(row["n"])

    def lapse_expired(self, now: dt.datetime) -> list[str]:
        rows = self.conn.execute(
            f"UPDATE {self.t('fo_plan')} SET status = 'LAPSED', reason = ? WHERE user_id = ? "
            "AND kind = 'ENTRY' AND status = 'ISSUED' AND expires_at <= ? RETURNING plan_id",
            ("not confirmed inside its window; a missed window is not retried on a later day "
             "(04 §1)", self.user_id, _aware(now)),
        ).fetchall()  # fmt: skip
        return [str(r["plan_id"]) for r in rows]

    def pending_actions(self) -> list[FoPlanRow]:
        """EXIT and ROLL plans the monitor raised whose position is still open."""
        rows = self.conn.execute(
            f"SELECT p.* FROM {self.t('fo_plan')} p WHERE p.user_id = ? AND p.kind IN "
            "('EXIT', 'ROLL') AND p.status IN ('ISSUED', 'CONFIRMED', 'FILLING') ORDER BY p.id",
            (self.user_id,),
        ).fetchall()
        return [self._plan(r) for r in rows]

    # fills ------------------------------------------------------------------------------------

    def write_fill(  # noqa: PLR0913 - one fo_fill row
        self, leg: FoLegRow, *, position_id: int | None, client_id: str, at: dt.datetime,
        side: str, quantity: int, price: Decimal, detail: Mapping[str, Any],
    ) -> None:  # fmt: skip
        self.conn.execute(
            f"INSERT INTO {self.t('fo_fill')} (user_id, leg_id, position_id, client_id, filled_at, "
            "side, quantity, price, simulated, sim_method, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, true, 'DEPTH_LADDER', ?)",
            (self.user_id, leg.id, position_id, client_id, _aware(at), side, quantity,
             money(price), dumps(dict(detail))),
        )  # fmt: skip

    def settle_legs(self, plan: FoPlanRow) -> None:
        """Each leg's filled quantity (in its own direction) and average price, from ``fo_fill``."""
        for leg in plan.legs:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(quantity), 0) AS q, SUM(quantity * price) AS v "
                f"FROM {self.t('fo_fill')} WHERE leg_id = ? AND side = ?",
                (leg.id, leg.side),
            ).fetchone()
            filled = int(row["q"] or 0)
            avg = None if not filled else money(Decimal(str(row["v"])) / filled)
            status = "FILLED" if filled >= leg.quantity else ("PARTIAL" if filled else "CANCELLED")
            self.conn.execute(
                f"UPDATE {self.t('fo_leg')} SET filled_qty = ?, avg_price = ?, status = ? "
                "WHERE id = ?",
                (min(filled, leg.quantity), avg, status, leg.id),
            )

    def attach_fills(self, plan: FoPlanRow, position_id: int) -> None:
        self.conn.execute(
            f"UPDATE {self.t('fo_fill')} SET position_id = ? WHERE leg_id = ANY(?) "
            "AND position_id IS NULL",
            (position_id, [lg.id for lg in plan.legs]),
        )

    def net_by_symbol(self, position_id: int) -> dict[str, int]:
        """Signed units per tradingsymbol from every fill of the position (buys +, sells -)."""
        rows = self.conn.execute(
            "SELECT l.tradingsymbol, COALESCE(SUM(CASE WHEN f.side = 'BUY' THEN f.quantity "
            f"ELSE -f.quantity END), 0) AS n FROM {self.t('fo_fill')} f "
            f"JOIN {self.t('fo_leg')} l ON l.id = f.leg_id WHERE f.position_id = ? "
            "GROUP BY l.tradingsymbol",
            (position_id,),
        ).fetchall()
        return {str(r["tradingsymbol"]): int(r["n"]) for r in rows}

    def plan_net(self, plan: FoPlanRow) -> dict[str, int]:
        """Signed units per tradingsymbol from this plan's own fills."""
        rows = self.conn.execute(
            "SELECT l.tradingsymbol, COALESCE(SUM(CASE WHEN f.side = 'BUY' THEN f.quantity "
            f"ELSE -f.quantity END), 0) AS n FROM {self.t('fo_fill')} f "
            f"JOIN {self.t('fo_leg')} l ON l.id = f.leg_id WHERE l.plan_id = ? "
            "GROUP BY l.tradingsymbol",
            (plan.pk,),
        ).fetchall()
        return {str(r["tradingsymbol"]): int(r["n"]) for r in rows}

    # positions --------------------------------------------------------------------------------

    def open_position(self, row: Mapping[str, Any]) -> int:
        cols = ["user_id", *row.keys()]
        values = [self.user_id, *(dumps(v) if k == "legs" else v for k, v in row.items())]
        out = self.conn.execute(
            f"INSERT INTO {self.t('fo_position')} ({', '.join(cols)}) VALUES "
            f"({', '.join('?' for _ in cols)}) RETURNING id",
            tuple(values),
        ).fetchone()
        return int(out["id"])

    def _position(self, r: Any) -> PositionRow:  # noqa: ANN401 - a row mapping
        return PositionRow(
            id=int(r["id"]), sleeve=FoSleeve(str(r["sleeve"])), symbol=str(r["symbol"]),
            structure=str(r["structure"]), entry_plan_id=str(r["entry_plan_id"]),
            legs=_json(r["legs"]), lots=int(r["lots"]), lot_size=int(r["lot_size"]),
            entry_price=_px(r["entry_price"]), entry_credit=_px(r["entry_credit"]),
            max_loss_inr=_px(r["max_loss_inr"]), stop_price=_px(r["stop_price"]),
            gtt_id=r["gtt_id"], hard_exit_date=_opt_day(r["hard_exit_date"]),
            next_roll_date=_opt_day(r["next_roll_date"]), opened_at=_aware(r["opened_at"]),
            simulated=bool(r["simulated"]),
        )  # fmt: skip

    def open_positions(self) -> list[PositionRow]:
        rows = self.conn.execute(
            f"SELECT * FROM {self.t('fo_position')} WHERE user_id = ? AND closed_at IS NULL "
            "ORDER BY id",
            (self.user_id,),
        ).fetchall()
        return [self._position(r) for r in rows]

    def position(self, position_id: int) -> PositionRow | None:
        r = self.conn.execute(
            f"SELECT * FROM {self.t('fo_position')} WHERE user_id = ? AND id = ?",
            (self.user_id, position_id),
        ).fetchone()
        return None if r is None else self._position(r)

    def update_position(self, position_id: int, **values: object) -> None:
        sets = ", ".join(f"{k} = ?" for k in values)
        params = [dumps(v) if k == "legs" else v for k, v in values.items()]
        self.conn.execute(
            f"UPDATE {self.t('fo_position')} SET {sets} WHERE user_id = ? AND id = ?",
            (*params, self.user_id, position_id),
        )

    def close_position(self, position_id: int, reason: str, at: dt.datetime) -> None:
        self.conn.execute(
            f"UPDATE {self.t('fo_position')} SET closed_at = ?, closed_reason = ? "
            "WHERE user_id = ? AND id = ? AND closed_at IS NULL",
            (_aware(at), reason[:32], self.user_id, position_id),
        )

    def write_mark(  # noqa: PLR0913 - one fo_mark row
        self, position_id: int, day: dt.date, *, mark_points: Decimal, pnl_inr: Decimal,
        stop_price: Decimal | None, detail: Mapping[str, Any],
    ) -> bool:  # fmt: skip
        """``True`` when this is the session's first mark for the position (idempotent)."""
        row = self.conn.execute(
            f"INSERT INTO {self.t('fo_mark')} (position_id, trade_date, user_id, mark_points, "
            "pnl_inr, stop_price, detail) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (position_id, trade_date) DO NOTHING RETURNING position_id",
            (position_id, day, self.user_id, money(mark_points), money(pnl_inr),
             None if stop_price is None else money(stop_price), dumps(dict(detail))),
        ).fetchone()  # fmt: skip
        return row is not None

    def marked_days(self, position_id: int) -> set[dt.date]:
        rows = self.conn.execute(
            f"SELECT trade_date FROM {self.t('fo_mark')} WHERE position_id = ?", (position_id,)
        ).fetchall()
        return {_day(r["trade_date"]) for r in rows}


# --- the gateway ---------------------------------------------------------------------------------


def fo_journal_path() -> str:
    from .core import gateway as _gateway_module  # noqa: PLC0415 - desk wiring only

    return os.path.join(os.path.dirname(_gateway_module.JOURNAL), "fno_orders_journal.jsonl")


def paper_gates(sleeve: FoSleeve) -> Any:  # noqa: ANN401 - a ProductGates
    """The sleeve's switches (``fno_gates.product_gates``) with ``dry_run`` pinned true (FO7.1)."""
    from .fno_gates import product_gates  # noqa: PLC0415

    return dataclasses.replace(product_gates(sleeve), dry_run=True)


def build_fo_gateway(sleeve: FoSleeve, kc: Any, risk: Any, journal_path: str) -> Any:  # noqa: ANN401
    """An FO gateway: the sleeve's paper-pinned switches and the covered-overnight rule wired."""
    from .core.gateway import OrderGateway  # noqa: PLC0415 - desk wiring only

    return OrderGateway(
        kc, risk, gates=lambda: paper_gates(sleeve), journal_path=journal_path, coverage=uncovered
    )


_GATEWAYS: dict[FoSleeve, Any] = {}


def fo_gateway(sleeve: FoSleeve, kc: Any, risk: Any) -> Any:  # noqa: ANN401
    """One per sleeve, sharing the desk's risk manager (one account, one loss cap)."""
    found = _GATEWAYS.get(sleeve)
    if found is None:
        found = build_fo_gateway(sleeve, kc, risk, fo_journal_path())
        _GATEWAYS[sleeve] = found
    return found


def fno_mode(sleeve: FoSleeve) -> Mode:
    from .fno_gates import fno_gates  # noqa: PLC0415

    return fno_gates(sleeve).mode


# --- the option venue: one attempt through the gateway, filled from the book ---------------------


def _client_id(plan_id: str, symbol: str, *, closing: bool, number: int) -> str:
    base = f"{plan_id}:{symbol}:CLOSE" if closing else f"{plan_id}:{symbol}"
    return base if number == 1 else f"{base}:R{number}"


def _option_leg(underlying: str, leg: FoLegRow, units: int) -> OptionPosition:
    if leg.strike is None:
        raise ValueError(f"{leg.tradingsymbol}: an option leg without a strike")
    return OptionPosition(underlying, leg.expiry, OptionType(leg.option_type), leg.strike, units)


class OptionVenue:
    """``baskfy_core.options.executor.Venue`` over the FO gateway (``DeskVenue``'s shape).

    Runs on a worker thread: ``send`` schedules ``gateway.place`` on the desk's loop and waits.
    ``held`` is the plan's legs as they stand (signed units per role), which is what each order's
    ``FoPlanRef.plan_filled`` carries; for an exit it starts as the position's open legs.
    """

    def __init__(  # noqa: PLR0913 - every collaborator one attempt needs
        self, *, gateway: Gateway, loop: asyncio.AbstractEventLoop, store: FoStore, plan: FoPlanRow,
        underlying: str, quotes: QuoteSource, now: Callable[[], dt.datetime],
        broker_book: BrokerBook, position_id: int | None, held: Mapping[str, int] | None = None,
        closing_is_exit: bool = False, execution: ExecutionConfig | None = None,
    ) -> None:  # fmt: skip
        self.gateway = gateway
        self.loop = loop
        self.store = store
        self.plan = plan
        self.underlying = underlying
        self.quotes = quotes
        self.now = now
        self.broker_book = broker_book
        self.position_id = position_id
        self.legs = {lg.role: lg for lg in plan.legs}
        self.held: dict[str, int] = dict(held or {})
        self.closing_is_exit = closing_is_exit
        self.execution = execution or ExecutionConfig()
        self.orders = 0
        self.refusals: list[str] = []

    def _book(self, symbol: str) -> FoQuote:
        quote = self.quotes([symbol]).get(symbol)
        if quote is None or not quote.two_sided:
            raise Refused(503, "NO_QUOTE", f"no two-sided quote for {symbol}")
        return quote

    def quote(self, role: LegRole) -> Book:
        q = self._book(self.legs[role.value].tradingsymbol)
        return Book(bid=q.bid or Decimal(0), ask=q.ask or Decimal(0))

    def _filled(self) -> tuple[OptionPosition, ...]:
        return tuple(
            _option_leg(self.underlying, self.legs[r], q) for r, q in self.held.items() if q
        )

    def send(self, role: LegRole, attempt: Attempt, quantity: int, closing: bool) -> Fill:
        leg = self.legs[role.value]
        # An exit plan's legs ARE closes: their client ids read as the exit plan's own orders.
        tagged_close = closing and not self.closing_is_exit
        key = f"{leg.tradingsymbol}:{'CLOSE' if tagged_close else 'OPEN'}"
        number = self.store.next_attempt(self.plan.pk, key)
        cid = _client_id(self.plan.plan_id, leg.tradingsymbol, closing=tagged_close, number=number)
        signed = quantity if attempt.side is Side.BUY else -quantity
        step = _option_leg(self.underlying, leg, signed)
        filled = self._filled()
        ref = FoPlanRef(
            plan_id=self.plan.plan_id, sleeve=self.plan.sleeve.value, step=step,
            broker_positions=self.broker_book(filled), plan_filled=filled,
        )  # fmt: skip
        book = self._book(leg.tradingsymbol)
        future = asyncio.run_coroutine_threadsafe(
            self.gateway.place(
                symbol=leg.tradingsymbol, qty=quantity, side=attempt.side.value, product="NRML",
                exchange="NFO", order_type="LIMIT", price=float(attempt.price), client_id=cid,
                gross_exposure=float(attempt.price) * quantity,
                reference_price=float((book.ask if attempt.side is Side.BUY else book.bid) or 0),
                fo_plan=ref,
            ),
            self.loop,
        )  # fmt: skip
        result = future.result()
        self.orders += 1
        status = str(result.get("status"))
        if status != DRY_RUN:
            self.refusals.append(f"{cid}: {status} {result.get('error') or ''}".strip())
            log.warning("fo leg %s %s: %s", cid, status, result.get("error"))
            return Fill(0, None)
        ladder = book.asks if attempt.side is Side.BUY else book.bids
        sim = simulate_fill(attempt.side, ladder, quantity, limit_price=attempt.price, tick=TICK,
                            config=self.execution)  # fmt: skip
        if sim.filled and sim.avg_price is not None:
            self.store.write_fill(
                leg, position_id=self.position_id, client_id=cid, at=self.now(),
                side=attempt.side.value, quantity=sim.filled, price=sim.avg_price,
                detail={"limit": attempt.price, "requested": quantity,
                        "levels_consumed": sim.levels_consumed, "marketable": attempt.marketable},
            )  # fmt: skip
            self.held[role.value] = self.held.get(role.value, 0) + (
                sim.filled if attempt.side is Side.BUY else -sim.filled
            )
        return Fill(sim.filled, sim.avg_price)


# --- the F2 leg: one future, bought or sold through the gateway ----------------------------------


async def _send_future(  # noqa: PLR0913 - the plan, the leg and the book
    store: FoStore, gateway: Gateway, plan: FoPlanRow, leg: FoLegRow, *, side: Side, quantity: int,
    closing: bool, quotes: QuoteSource, now: Callable[[], dt.datetime], position_id: int | None,
    execution: ExecutionConfig,
) -> tuple[int, Decimal | None, list[str]]:  # fmt: skip
    """Attempts until ``quantity`` fills or they run out: two limits, and a third marketable one
    for a sale (it reduces risk). Returns (filled, average price, refusals)."""
    remaining, value, refusals = quantity, Decimal(0), []
    ref = FoPlanRef(plan_id=plan.plan_id, sleeve=plan.sleeve.value)
    for n in range(1, 4):
        book = quotes([leg.tradingsymbol]).get(leg.tradingsymbol)
        if book is None or not book.two_sided or book.bid is None or book.ask is None:
            refusals.append(f"{leg.tradingsymbol}: no two-sided quote")
            break
        attempt = next_attempt(n, side, book.bid, book.ask, TICK,
                               closing_reduces_risk=closing and side is Side.SELL,
                               config=execution)  # fmt: skip
        if attempt is None:
            break
        key = f"{leg.tradingsymbol}:{'CLOSE' if closing else 'OPEN'}"
        number = store.next_attempt(plan.pk, key)
        cid = _client_id(plan.plan_id, leg.tradingsymbol, closing=closing, number=number)
        result = await gateway.place(
            symbol=leg.tradingsymbol, qty=remaining, side=side.value, product="NRML",
            exchange="NFO", order_type="LIMIT", price=float(attempt.price), client_id=cid,
            gross_exposure=float(attempt.price) * remaining,
            reference_price=float(book.ask if side is Side.BUY else book.bid), fo_plan=ref,
        )  # fmt: skip
        status = str(result.get("status"))
        if status != DRY_RUN:
            refusals.append(f"{cid}: {status} {result.get('error') or ''}".strip())
            break
        ladder = book.asks if side is Side.BUY else book.bids
        sim = simulate_fill(side, ladder, remaining, limit_price=attempt.price, tick=TICK,
                            config=execution)  # fmt: skip
        if sim.filled and sim.avg_price is not None:
            store.write_fill(
                leg, position_id=position_id, client_id=cid, at=now(), side=side.value,
                quantity=sim.filled, price=sim.avg_price,
                detail={"limit": attempt.price, "requested": remaining,
                        "levels_consumed": sim.levels_consumed, "marketable": attempt.marketable},
            )  # fmt: skip
            value += sim.avg_price * sim.filled
            remaining -= sim.filled
        if remaining == 0:
            break
    filled = quantity - remaining
    return filled, (None if not filled else money(value / filled)), refusals


# --- F2's stop: placed the fill's session, moved each evening, never lowered ---------------------


def _tenancy() -> dict[str, Any]:
    """The GTT methods take the tenant explicitly (the desk shim stamps it for ``place`` only);
    the console trades one account, the same stamp ``place`` gets (P4.3)."""
    from baskfy_execution.tenancy import TenantIds  # noqa: PLC0415

    from . import config as C  # noqa: PLC0415

    sole = TenantIds(user_id=int(C.SOLE_USER_ID), broker_account_id=int(C.SOLE_BROKER_ACCOUNT_ID))
    return {"tenant": sole, "plan_tenant": sole}


def paper_gtt_handle(position_id: int) -> str:
    """A paper GTT has no exchange id; its handle is the position's (FO7.5)."""
    return f"PAPER-{position_id}"


def gtt_handle_int(gtt_id: str | None) -> int | None:
    """The trigger id the gateway addresses: the exchange's, or a paper handle's position id."""
    if not gtt_id:
        return None
    raw = gtt_id.removeprefix("PAPER-")
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


async def place_future_stop(  # noqa: PLR0913 - the position and its stop
    store: FoStore, gateway: Gateway, *, position_id: int, plan: FoPlanRow, symbol: str, qty: int,
    trigger: Decimal, last_price: Decimal, now: dt.datetime, alert: Callable[[str], None],
) -> str | None:  # fmt: skip
    """``04`` §10: the GTT in the fill's session, through the gateway's F2 branch. A GTT that fails
    to place is an alert and a ``NAKED_FUTURE`` violation; the monitor exits at the next check."""
    result = await gateway.place_gtt_stop(
        symbol=symbol, qty=qty, trigger=float(trigger), last_price=float(last_price),
        exchange="NFO", client_id=f"{plan.plan_id}:{symbol}:GTT",
        fo_plan=FoPlanRef(plan_id=plan.plan_id, sleeve=plan.sleeve.value), **_tenancy(),
    )  # fmt: skip
    status = str(result.get("status"))
    if status in GTT_PLACED_STATUSES or status == _DUPLICATE:
        raw = str(result.get("gtt_id") or "")
        handle = raw if raw.isdigit() else paper_gtt_handle(position_id)
        store.update_position(position_id, gtt_id=handle, stop_price=money(trigger))
        return handle
    message = f"F2 {symbol}: the GTT stop did not place ({status}: {result.get('error')})"
    alert(message)
    entry_plan = plan.parent_plan_id or plan.plan_id
    store.add_violation(entry_plan, "NAKED_FUTURE", message, now)
    store.update_position(position_id, gtt_id=None)
    return None


async def move_future_stop(  # noqa: PLR0913 - the position and its new trigger
    store: FoStore, gateway: Gateway, *, position: PositionRow, symbol: str, qty: int,
    trigger: Decimal, floor: Decimal, last_price: Decimal,
) -> str:  # fmt: skip
    """The evening modify (FO6.4's gap, closed in FO7): the resting trigger moves up to
    ``trigger`` through ``modify_gtt_quantity`` with the F2 reference and ``floor_trigger``."""
    handle = gtt_handle_int(position.gtt_id)
    if handle is None:
        return "NO_GTT"
    result = await gateway.modify_gtt_quantity(
        gtt_id=handle, symbol=symbol, qty=qty, trigger=float(trigger),
        last_price=float(last_price), exchange="NFO",
        client_id=f"{position.entry_plan_id}:{symbol}:TRAIL:{trigger}",
        fo_plan=FoPlanRef(plan_id=position.entry_plan_id, sleeve=position.sleeve.value),
        floor_trigger=float(floor), **_tenancy(),
    )  # fmt: skip
    status = str(result.get("status"))
    if status in GTT_MODIFIED_STATUSES:
        store.update_position(position.id, stop_price=money(trigger))
    else:
        log.error("F2 %s: the trail modify was refused (%s: %s)", symbol, status,
                  result.get("error"))  # fmt: skip
    return status


async def delete_future_stop(gateway: Gateway, position: PositionRow, symbol: str) -> None:
    """A real resting trigger is cancelled when its position closes; a paper one rests nowhere."""
    if position.gtt_id is None or position.gtt_id.startswith("PAPER-"):
        return
    handle = gtt_handle_int(position.gtt_id)
    if handle is not None:
        await gateway.delete_gtt(gtt_id=handle, symbol=symbol, exchange="NFO",
                                 client_id=f"{position.entry_plan_id}:{symbol}:GTT-DEL",
                                 **_tenancy())  # fmt: skip


# --- the confirm (FO8 calls this) ----------------------------------------------------------------


def _validate(plan: FoPlanRow | None, confirm: bool, now: dt.datetime) -> FoPlanRow:
    if not confirm:
        raise Refused(400, "CONFIRM_REQUIRED", "confirm must be true")
    if plan is None:
        raise Refused(404, "UNKNOWN_PLAN", "no such plan")
    if plan.kind != PlanKind.ENTRY.value:
        raise Refused(400, "NOT_AN_ENTRY", "only an entry plan is confirmed")
    if plan.status != PlanState.ISSUED.value:
        raise Refused(409, "NOT_ISSUED", f"the plan is {plan.status}")
    if now >= plan.expires_at:
        raise Refused(410, "PLAN_EXPIRED", "the plan's 30-minute window has passed")
    return plan


async def execute_entry(  # noqa: PLR0913 - the store, the gateway, the book, the plan, the clock
    store: FoStore,
    gateway: Gateway,
    *,
    quotes: QuoteSource,
    plan_id: str,
    confirm: bool,
    mode_of: Callable[[FoSleeve], Mode] = fno_mode,
    now: Callable[[], dt.datetime] | None = None,
    broker_book: BrokerBook = paper_book,
    alert: Callable[[str], None] = log.error,
) -> FoOutcome:
    """``POST /fno/execute {plan_id, confirm: true}``: refuse, or send the legs in ``entry_seq``.

    F1: longs first through ``run_entry``; a leg short of its quantity abandons the entry before
    any short and closes the filled longs at once (``ABANDONED_PARTIAL``). F2: the future, then
    its GTT in the same session. Every order simulated (module docstring)."""
    clock = now or (lambda: dt.datetime.now(tz=IST))
    plan = _validate(store.plan(plan_id), confirm, clock())
    if mode_of(plan.sleeve) is Mode.LIVE:
        raise Refused(409, "LIVE_NOT_BUILT", "live FO execution is not built (FO7.1); nothing sent")
    for leg in plan.legs:  # every leg quotable before the first order, or nothing is sent
        book = quotes([leg.tradingsymbol]).get(leg.tradingsymbol)
        if book is None or not book.two_sided:
            raise Refused(503, "NO_QUOTE", f"no two-sided quote for {leg.tradingsymbol}")
    if not store.confirm(plan, clock()):
        raise Refused(409, "NOT_ISSUED", "the plan was confirmed or closed meanwhile")
    store.set_status(plan.plan_id, PlanState.FILLING.value)
    if plan.structure == Structure.FUTURE.value:
        return await _enter_future(store, gateway, plan, quotes=quotes, now=clock, alert=alert)
    return await _enter_condor(store, gateway, plan, quotes=quotes, now=clock,
                               broker_book=broker_book, alert=alert)  # fmt: skip


def _legs_json(plan: FoPlanRow, avg: Mapping[str, Decimal | None]) -> list[dict[str, Any]]:
    return [
        {"role": lg.role, "tradingsymbol": lg.tradingsymbol,
         "instrument_token": lg.instrument_token,
         "expiry": lg.expiry, "strike": lg.strike, "option_type": lg.option_type,
         "quantity": lg.quantity, "avg_price": avg.get(lg.role)}
        for lg in plan.legs
    ]  # fmt: skip


async def _enter_condor(  # noqa: PLR0913 - the plan and its collaborators
    store: FoStore, gateway: Gateway, plan: FoPlanRow, *, quotes: QuoteSource,
    now: Callable[[], dt.datetime], broker_book: BrokerBook, alert: Callable[[str], None],
) -> FoOutcome:  # fmt: skip
    venue = OptionVenue(gateway=gateway, loop=asyncio.get_running_loop(), store=store, plan=plan,
                        underlying=plan.symbol, quotes=quotes, now=now, broker_book=broker_book,
                        position_id=None)  # fmt: skip
    quantity = plan.lots * plan.lot_size
    roles = [LegRole(r.value) for r in ENTRY_SEQUENCE if r.value in venue.legs]
    result = await asyncio.to_thread(
        run_entry, venue, roles, quantity, sleeve=_CONDOR_RULES, tick=TICK,
        config=ExecutionConfig(),
    )  # fmt: skip
    store.settle_legs(plan)
    if result.outcome is Outcome.OPEN:
        avg = {r.value: result.avg_price(r) for r in result.fills}
        credit = Decimal(0)
        for role, price in avg.items():
            credit += (-(price or 0)) if LegRole(role).is_long else (price or 0)
        credit = money(credit)
        detail = plan.detail
        width = _dec(detail.get("width_points")) or Decimal(0)
        max_loss = money((width - credit) * quantity) if width else None
        levels = _exit_levels(detail.get("strikes"), credit)
        position_id = store.open_position({
            "sleeve": plan.sleeve.value, "symbol": plan.symbol, "structure": plan.structure,
            "entry_plan_id": plan.plan_id,
            "legs": {"legs": _legs_json(plan, avg), "carry": {"strikes": detail.get("strikes")}},
            "lots": plan.lots, "lot_size": plan.lot_size, "entry_credit": credit,
            "max_loss_inr": max_loss,
            "profit_take_points": levels[0], "loss_close_points": levels[1],
            "hard_exit_date": plan.hard_exit_date, "opened_at": now(), "simulated": True,
        })  # fmt: skip
        store.attach_fills(plan, position_id)
        store.set_status(plan.plan_id, PlanState.OPEN.value)
        return FoOutcome(plan.plan_id, plan.sleeve.value, plan.kind, "OPEN", True, venue.orders,
                         position_id, {"credit": credit})  # fmt: skip
    left = {r.value: q for r, q in result.position.items() if q}
    reason = ("a leg did not fill in full; the entry was abandoned before any short was left "
              "uncovered and the filled longs were closed at once")  # fmt: skip
    if venue.refusals:
        reason += f" ({'; '.join(venue.refusals)})"
    store.set_status(plan.plan_id, PlanState.ABANDONED_PARTIAL.value, reason)
    store.merge_detail(plan.plan_id, {"left_open": left, "refusals": venue.refusals})
    if left:
        alert(f"FO {plan.plan_id}: abandoned entry left {left} open; close it by hand")
    return FoOutcome(plan.plan_id, plan.sleeve.value, plan.kind,
                     PlanState.ABANDONED_PARTIAL.value, True, venue.orders,
                     detail={"left_open": left, "refusals": venue.refusals})  # fmt: skip


def _exit_levels(raw: object, credit: Decimal) -> tuple[Decimal | None, Decimal | None]:
    """The profit-take and loss-close costs to close on the credit actually taken (``04`` §1)."""
    from baskfy_core.fno.condor import (  # noqa: PLC0415
        CondorStrikes,
        loss_close_level,
        profit_take_level,
    )
    from baskfy_core.fno.config import DEFAULT_FNO_CONFIG  # noqa: PLC0415

    if not isinstance(raw, dict) or credit <= 0:
        return None, None
    try:
        strikes = CondorStrikes(*(Decimal(str(raw[k])) for k in
                                  ("LONG_CALL", "SHORT_CALL", "SHORT_PUT", "LONG_PUT")))
    except (KeyError, ArithmeticError, ValueError):
        return None, None  # fmt: skip
    f1 = DEFAULT_FNO_CONFIG.f1
    return (money(profit_take_level(credit, f1)),
            money(loss_close_level(credit, strikes, f1)))  # fmt: skip


async def _enter_future(  # noqa: PLR0913 - the plan and its collaborators
    store: FoStore, gateway: Gateway, plan: FoPlanRow, *, quotes: QuoteSource,
    now: Callable[[], dt.datetime], alert: Callable[[str], None],
) -> FoOutcome:  # fmt: skip
    leg = plan.legs[0]
    execution = ExecutionConfig()
    filled, avg, refusals = await _send_future(
        store, gateway, plan, leg, side=Side.BUY, quantity=leg.quantity, closing=False,
        quotes=quotes, now=now, position_id=None, execution=execution,
    )  # fmt: skip
    orders = len(refusals) + (1 if filled else 0)
    if filled < leg.quantity or avg is None:
        if filled:  # a partial future is sold back at once: no half-sized carry
            await _send_future(store, gateway, plan, leg, side=Side.SELL, quantity=filled,
                               closing=True, quotes=quotes, now=now, position_id=None,
                               execution=execution)  # fmt: skip
        store.settle_legs(plan)
        reason = f"the future filled {filled} of {leg.quantity}; the entry was abandoned"
        if refusals:
            reason += f" ({'; '.join(refusals)})"
        store.set_status(plan.plan_id, PlanState.ABANDONED_PARTIAL.value, reason)
        return FoOutcome(plan.plan_id, plan.sleeve.value, plan.kind,
                         PlanState.ABANDONED_PARTIAL.value, True, orders,
                         detail={"refusals": refusals})  # fmt: skip
    store.settle_legs(plan)
    d = plan.detail
    atr = _dec(d.get("atr14")) or Decimal(0)
    entry = avg
    from baskfy_core.fno.config import DEFAULT_FNO_CONFIG  # noqa: PLC0415
    from baskfy_core.fno.exits import f2_initial_stop, gtt_trigger  # noqa: PLC0415

    cfg = DEFAULT_FNO_CONFIG
    ann_vol = float(d.get("ann_vol") or 0.0)
    stop = money(f2_initial_stop(entry, atr, cfg.f2))
    trigger = money(gtt_trigger(stop, entry, ann_vol, cfg.stop_vol))
    carry = {
        "trail_stop": stop, "highest_close": entry, "atr_at_entry": atr, "ann_vol": ann_vol,
        "entry_session": plan.trade_date, "time_exit_date": d.get("time_exit_date"),
        "contract_expiry": leg.expiry, "rolls": 0, "realised_inr": "0",
        "lots_at_ceiling": d.get("lots_at_ceiling"),
    }  # fmt: skip
    position_id = store.open_position({
        "sleeve": plan.sleeve.value, "symbol": plan.symbol, "structure": plan.structure,
        "entry_plan_id": plan.plan_id,
        "legs": {"legs": _legs_json(plan, {leg.role: entry}), "carry": carry},
        "lots": plan.lots, "lot_size": plan.lot_size, "entry_price": entry,
        "max_loss_inr": money((entry - trigger) * leg.quantity), "stop_price": trigger,
        "next_roll_date": _opt_day(d.get("roll_date")), "opened_at": now(), "simulated": True,
    })  # fmt: skip
    store.attach_fills(plan, position_id)
    store.set_status(plan.plan_id, PlanState.OPEN.value)
    book = quotes([leg.tradingsymbol]).get(leg.tradingsymbol)
    last = (book.last or book.mid) if book is not None else None
    handle = await place_future_stop(
        store, gateway, position_id=position_id, plan=plan, symbol=leg.tradingsymbol,
        qty=leg.quantity, trigger=trigger, last_price=last or entry, now=now(), alert=alert,
    )  # fmt: skip
    return FoOutcome(plan.plan_id, plan.sleeve.value, plan.kind, "OPEN", True, orders + 1,
                     position_id, {"entry": entry, "trigger": trigger, "gtt": handle})  # fmt: skip


# --- exits and rolls under the entry's confirm ---------------------------------------------------


def _position_for(store: FoStore, plan: FoPlanRow) -> PositionRow | None:
    raw = plan.detail.get("position_id")
    return None if raw is None else store.position(int(raw))


async def execute_exit(  # noqa: PLR0913 - the store, the gateway, the plan and the clock
    store: FoStore,
    gateway: Gateway,
    *,
    plan: FoPlanRow,
    quotes: QuoteSource,
    now: Callable[[], dt.datetime],
    broker_book: BrokerBook = paper_book,
) -> FoOutcome:
    """Close a position, shorts first (``04`` §2). ``CLOSED`` when flat; otherwise the plan stays
    ``FILLING`` and the next pass tries again — never naked."""
    position = _position_for(store, plan)
    if position is None:
        store.set_status(plan.plan_id, PlanState.CLOSED.value, "no open position to close")
        return FoOutcome(plan.plan_id, plan.sleeve.value, plan.kind, "NO_POSITION", True, 0)
    store.set_status(plan.plan_id, PlanState.FILLING.value)
    store.set_status(position.entry_plan_id, PlanState.EXITING.value, only_from=("OPEN",))
    net = store.net_by_symbol(position.id)
    orders = 0
    if position.structure == Structure.IRON_CONDOR.value:
        by_symbol = {lg.tradingsymbol: lg.role for lg in plan.legs}
        held = {by_symbol[s]: q for s, q in net.items() if s in by_symbol}
        venue = OptionVenue(gateway=gateway, loop=asyncio.get_running_loop(), store=store,
                            plan=plan, underlying=position.symbol, quotes=quotes, now=now,
                            broker_book=broker_book, position_id=position.id, held=held,
                            closing_is_exit=True)  # fmt: skip
        open_qty = {LegRole(r): abs(q) for r, q in held.items() if q}
        result = await asyncio.to_thread(
            run_exit, venue, open_qty, sleeve=_CONDOR_RULES, tick=TICK, config=ExecutionConfig()
        )
        orders = venue.orders
        flat = result.outcome is Outcome.FLAT
    else:
        for leg in plan.legs:
            units = net.get(leg.tradingsymbol, 0)
            if units > 0:
                _filled, _avg, refusals = await _send_future(
                    store, gateway, plan, leg, side=Side.SELL, quantity=units, closing=True,
                    quotes=quotes, now=now, position_id=position.id, execution=ExecutionConfig(),
                )  # fmt: skip
                orders += 1 + len(refusals)
        flat = all(q == 0 for q in store.net_by_symbol(position.id).values())
    store.settle_legs(plan)
    if not flat:
        return FoOutcome(plan.plan_id, plan.sleeve.value, plan.kind, "PARTIAL_EXIT", True, orders,
                         position.id)  # fmt: skip
    reason = str(plan.detail.get("code") or "EXIT")
    store.close_position(position.id, reason, now())
    store.set_status(plan.plan_id, PlanState.CLOSED.value)
    store.set_status(position.entry_plan_id, PlanState.CLOSED.value, only_from=("OPEN", "EXITING"))
    if position.structure == Structure.FUTURE.value and reason != "STOP":
        for leg in plan.legs:
            await delete_future_stop(gateway, position, leg.tradingsymbol)
    return FoOutcome(plan.plan_id, plan.sleeve.value, plan.kind, "CLOSED", True, orders,
                     position.id, {"reason": reason})  # fmt: skip


async def execute_roll(  # noqa: PLR0913 - the roll's two legs and its stop
    store: FoStore,
    gateway: Gateway,
    *,
    plan: FoPlanRow,
    quotes: QuoteSource,
    now: Callable[[], dt.datetime],
    alert: Callable[[str], None] = log.error,
) -> FoOutcome:
    """F2's calendar roll (``02`` Track C §5's one exception): the held month sold, the next bought,
    same quantity, stop carried, under the original confirm. ``ISSUED → FILLING → OPEN``."""
    position = _position_for(store, plan)
    if position is None or len(plan.legs) != 2:  # noqa: PLR2004 - sell the held, buy the next
        store.set_status(plan.plan_id, PlanState.CLOSED.value, "no open position to roll")
        return FoOutcome(plan.plan_id, plan.sleeve.value, plan.kind, "NO_POSITION", True, 0)
    store.set_status(plan.plan_id, PlanState.FILLING.value)
    old, new = plan.legs
    execution = ExecutionConfig()
    net = store.net_by_symbol(position.id)
    orders = 0
    if net.get(old.tradingsymbol, 0) > 0:
        _f, _a, refusals = await _send_future(
            store, gateway, plan, old, side=Side.SELL, quantity=net[old.tradingsymbol],
            closing=True, quotes=quotes, now=now, position_id=position.id, execution=execution,
        )  # fmt: skip
        orders += 1 + len(refusals)
        if store.net_by_symbol(position.id).get(old.tradingsymbol, 0) > 0:
            store.settle_legs(plan)
            return FoOutcome(plan.plan_id, plan.sleeve.value, plan.kind, "PARTIAL_ROLL", True,
                             orders, position.id)  # fmt: skip
    bought = store.net_by_symbol(position.id).get(new.tradingsymbol, 0)
    refusals = []
    avg: Decimal | None = None
    if bought < new.quantity:
        _filled, avg, refusals = await _send_future(
            store, gateway, plan, new, side=Side.BUY, quantity=new.quantity - bought,
            closing=False, quotes=quotes, now=now, position_id=position.id, execution=execution,
        )  # fmt: skip
        orders += 1 + len(refusals)
    store.settle_legs(plan)
    bought = store.net_by_symbol(position.id).get(new.tradingsymbol, 0)
    if bought < new.quantity:
        if bought > 0:  # a part-bought next month is sold back: no half-sized carry
            await _send_future(store, gateway, plan, new, side=Side.SELL, quantity=bought,
                               closing=True, quotes=quotes, now=now, position_id=position.id,
                               execution=execution)  # fmt: skip
        reason = f"the roll sold {old.tradingsymbol} but bought {bought} of {new.quantity}"
        store.set_status(plan.plan_id, PlanState.ABANDONED_PARTIAL.value,
                         reason + (f" ({'; '.join(refusals)})" if refusals else ""))  # fmt: skip
        store.close_position(position.id, "ROLL_INCOMPLETE", now())
        store.set_status(position.entry_plan_id, PlanState.CLOSED.value, only_from=("OPEN",))
        await delete_future_stop(gateway, position, old.tradingsymbol)
        alert(f"F2 {position.symbol}: {reason}; the position is flat")
        return FoOutcome(plan.plan_id, plan.sleeve.value, plan.kind, "ROLL_INCOMPLETE", True,
                         orders, position.id)  # fmt: skip
    old_leg = next((x for x in position.leg_list if x.get("tradingsymbol") == old.tradingsymbol),
                   {})  # fmt: skip
    old_entry = _dec(old_leg.get("avg_price")) or position.entry_price or Decimal(0)
    sold = _fill_avg(store, old, "SELL")
    carry = position.carry
    realised = (_dec(carry.get("realised_inr")) or Decimal(0)) + (
        (sold - old_entry) * old.quantity if sold is not None else Decimal(0))
    new_avg = avg or _fill_avg(store, new, "BUY") or Decimal(0)
    carry.update({"rolls": int(carry.get("rolls") or 0) + 1, "realised_inr": money(realised),
                  "contract_expiry": new.expiry})  # fmt: skip
    legs = [{"role": FUTURE_ROLE, "tradingsymbol": new.tradingsymbol,
             "instrument_token": new.instrument_token, "expiry": new.expiry, "strike": None,
             "option_type": FUTURE_TYPE, "quantity": new.quantity, "avg_price": new_avg}]
    store.update_position(position.id, legs={"legs": legs, "carry": carry},
                          next_roll_date=_opt_day(plan.detail.get("next_roll_date")))  # fmt: skip
    store.set_status(plan.plan_id, PlanState.OPEN.value)
    await delete_future_stop(gateway, position, old.tradingsymbol)
    trigger = position.stop_price or Decimal(0)
    book = quotes([new.tradingsymbol]).get(new.tradingsymbol)
    last = (book.last or book.mid) if book is not None else None
    await place_future_stop(
        store, gateway, position_id=position.id, plan=plan, symbol=new.tradingsymbol,
        qty=new.quantity, trigger=trigger, last_price=last or new_avg, now=now(), alert=alert,
    )  # fmt: skip
    return FoOutcome(plan.plan_id, plan.sleeve.value, plan.kind, "OPEN", True, orders,
                     position.id, {"rolled_to": new.tradingsymbol})  # fmt: skip


def _fill_avg(store: FoStore, leg: FoLegRow, side: str) -> Decimal | None:
    row = store.conn.execute(
        f"SELECT COALESCE(SUM(quantity), 0) AS q, SUM(quantity * price) AS v "
        f"FROM {store.t('fo_fill')} WHERE leg_id = ? AND side = ?",
        (leg.id, side),
    ).fetchone()
    q = int(row["q"] or 0)
    return None if not q else money(Decimal(str(row["v"])) / q)


async def run_pending(  # noqa: PLR0913 - the store, the gateways, the book and the clock
    store: FoStore,
    gateway_for: Callable[[FoSleeve], Any],
    *,
    quotes: QuoteSource,
    now: Callable[[], dt.datetime],
    broker_book: BrokerBook = paper_book,
    alert: Callable[[str], None] = log.error,
) -> list[FoOutcome]:
    """Every EXIT and ROLL plan the monitor raised, sent under its entry's confirm. A plan that
    cannot get a quote this pass waits for the next."""
    done: list[FoOutcome] = []
    for plan in store.pending_actions():
        try:
            if plan.kind == PlanKind.ROLL.value:
                done.append(await execute_roll(store, gateway_for(plan.sleeve), plan=plan,
                                               quotes=quotes, now=now, alert=alert))  # fmt: skip
            else:
                done.append(await execute_exit(store, gateway_for(plan.sleeve), plan=plan,
                                               quotes=quotes, now=now,
                                               broker_book=broker_book))  # fmt: skip
        except Refused as exc:
            log.warning("fo %s waits: %s", plan.plan_id, exc.message)
    return done

