"""The options monitor's runner: `python -m app.options_monitor` (OP9).

The strategy (`app/strategies/nifty_options.py`) decides; this module is everything around it that
touches the world — the flag, the database, the broker's quotes and the websocket — so the strategy
can be replayed from a file (`tools/options/replay.py`).

THE FLAG. `build_monitor` returns ``None`` when `BASKFY_OPTIONS_MONITOR_ENABLED` is false and the
strategy class is not constructed at all (OP9 AC: "flag off → not instantiated"); `main` returns 0
before importing the database, the ticker or Kite.

THE STORE. `PgPositionStore` reads and writes the screener's `op_` tables (schema `public`) through
the desk's own synchronous Postgres adapter (`app.analytics.db.connect`, `search_path=desk`), the
way `PgSignalStore` writes `sw_`:

* `open_positions(day)` — every `op_position` of the day with no `closed_at`, joined to its session
  and entry plan, with each leg's filled quantity and average price, and the plan's invalidation
  numbers (the opening range for O2, the morning range for O3-A, `half_gap` for O3-B);
* `raise_exit(tracked, verdict, at)` — one `op_plan` of `kind='EXIT'` (plan id
  `<entry plan>-X`, deterministic, so a second write is a no-op) and `op_position.exit_plan_id`,
  set only where it is still null. OP10's executor reads it and sends the closes under the entry's
  confirm (PACK.2);
* `record_mark` — `op_position.last_mark_points` / `last_mark_at`;
* `index_minutes` — `op_index_minute` for NIFTY 50, the bars the monitor reconciles to.

NO GATEWAY. The process holds none: `build_monitor(..., gateway=None)`. Nothing here places,
modifies or cancels an order, and a source scan in `tests/test_options_monitor.py` keeps it so.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
from decimal import Decimal
from typing import Any

from baskfy_core.options.bars import Bar
from baskfy_core.options.config import Sleeve
from baskfy_core.options.execution import LegRole
from baskfy_core.options.exits import ExitVerdict, OpenLeg, OpenPosition
from baskfy_core.options.ledger import pnl_points
from baskfy_core.options.structures import Direction, Structure

from . import config as C
from .strategies.nifty_options import NIFTY_50_TOKEN, NiftyOptionsMonitor, TrackedPosition

log = logging.getLogger("desk.options_monitor")

SCHEMA = "public"
SESSION_CLOSE = dt.time(15, 30)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _t(schema: str, table: str) -> str:
    return f"{schema}.{table}" if schema else table


def _d(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _aware(at: dt.datetime | str) -> dt.datetime:
    """The desk's adapter hands timestamps back as ISO strings (`analytics/pg.py`, sqlite's shape);
    a caller may pass datetimes. Either way: an aware datetime."""
    value = dt.datetime.fromisoformat(at) if isinstance(at, str) else at
    return value if value.tzinfo is not None else value.replace(tzinfo=IST)


def _detail(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}
    return {}


def position_from_rows(head: Any, legs: list[Any]) -> TrackedPosition:
    """One `op_position` (joined to its session and entry plan) and its legs, as the pure view
    `exits.evaluate` reads. Kept separate from the SQL so it is tested without a database."""
    detail = _detail(head["detail"])
    gate = detail.get("gate") if isinstance(detail.get("gate"), dict) else {}
    sleeve = Sleeve(str(head["sleeve"]))
    direction_raw = detail.get("direction")
    direction = Direction(str(direction_raw)) if direction_raw else None
    if sleeve is Sleeve.O2:
        high, low = _d(gate.get("or_high")), _d(gate.get("or_low"))
    else:
        high, low = _d(gate.get("range_high")), _d(gate.get("range_low"))
    open_legs = tuple(
        OpenLeg(
            role=LegRole(str(leg["role"])),
            strike=Decimal(str(leg["strike"])),
            quantity=int(leg["filled_qty"] or leg["quantity"]),
            fill_price=Decimal(str(leg["avg_price"] if leg["avg_price"] is not None else 0)),
            instrument_token=int(leg["instrument_token"]),
        )
        for leg in legs
    )
    position = OpenPosition(
        sleeve=sleeve,
        structure=Structure(str(head["structure"])),
        legs=open_legs,
        entry_points=Decimal(str(head["entry_points"])),
        opened_at=_aware(head["opened_at"]),
        risk_budget_inr=Decimal(str(head["risk_budget_inr"] or 0)),
        direction=direction,
        range_high=high,
        range_low=low,
        half_gap=_d(gate.get("half_gap")),
        width_points=_d(head["width_points"]),
    )
    return TrackedPosition(
        session_id=int(head["session_id"]),
        position=position,
        exit_plan_id=head["exit_plan_id"],
    )


class PgPositionStore:
    """The monitor's `PositionStore` over the `op_` tables. Every statement names its schema."""

    def __init__(self, conn: Any, *, user_id: int, schema: str = SCHEMA) -> None:
        self.conn = conn
        self.user_id = user_id
        self.schema = schema

    def open_positions(self, day: dt.date) -> list[TrackedPosition]:
        s = self.schema
        heads = self.conn.execute(
            "SELECT p.session_id, p.entry_points, p.opened_at, p.exit_plan_id, p.leg_ids, "
            "s.sleeve, pl.structure, pl.risk_budget_inr, pl.width_points, pl.detail "
            f"FROM {_t(s, 'op_position')} p "
            f"JOIN {_t(s, 'op_session')} s ON s.id = p.session_id "
            f"JOIN {_t(s, 'op_plan')} pl ON pl.plan_id = s.plan_id "
            "WHERE p.user_id = ? AND s.trade_date = ? AND p.closed_at IS NULL "
            "ORDER BY p.session_id",
            (self.user_id, day),
        ).fetchall()
        out: list[TrackedPosition] = []
        for head in heads:
            legs = self.conn.execute(
                "SELECT id, role, strike, quantity, filled_qty, avg_price, instrument_token "
                f"FROM {_t(s, 'op_leg')} WHERE id = ANY(?) ORDER BY seq",
                (list(head["leg_ids"]),),
            ).fetchall()
            out.append(position_from_rows(head, list(legs)))
        return out

    def raise_exit(
        self, tracked: TrackedPosition, verdict: ExitVerdict, at: dt.datetime
    ) -> str | None:
        s = self.schema
        entry = self.conn.execute(
            f"SELECT pl.plan_id, pl.sleeve, pl.structure, pl.sizing_mode, pl.lots, pl.lot_size "
            f"FROM {_t(s, 'op_session')} se JOIN {_t(s, 'op_plan')} pl ON pl.plan_id = se.plan_id "
            "WHERE se.id = ?",
            (tracked.session_id,),
        ).fetchone()
        if entry is None:
            log.error("options exit: session %s has no entry plan", tracked.session_id)
            return None
        exit_id = f"{entry['plan_id']}-X"
        issued = _aware(at)
        close = dt.datetime.combine(issued.date(), SESSION_CLOSE, tzinfo=IST)
        expires = max(close, issued + dt.timedelta(minutes=1))
        detail = {
            "code": verdict.code,
            "reason": verdict.reason,
            "value_points": None if verdict.value_points is None else str(verdict.value_points),
            "marked_loss_inr": None
            if verdict.marked_loss_inr is None
            else str(verdict.marked_loss_inr.quantize(Decimal("0.01"))),
            "stale": verdict.stale,
            "entry_plan_id": entry["plan_id"],
        }
        self.conn.execute(
            f"INSERT INTO {_t(s, 'op_plan')} (user_id, plan_id, session_id, sleeve, structure, "
            "kind, sizing_mode, issued_at, expires_at, lots, lot_size, status, detail) "
            "VALUES (?, ?, ?, ?, ?, 'EXIT', ?, ?, ?, ?, ?, 'ISSUED', ?) "
            "ON CONFLICT (plan_id) DO NOTHING",
            (
                self.user_id,
                exit_id,
                tracked.session_id,
                entry["sleeve"],
                entry["structure"],
                entry["sizing_mode"],
                issued,
                expires,
                entry["lots"],
                entry["lot_size"],
                json.dumps(detail),
            ),
        )
        self.conn.execute(
            f"UPDATE {_t(s, 'op_position')} SET exit_plan_id = ? "
            "WHERE session_id = ? AND exit_plan_id IS NULL",
            (exit_id, tracked.session_id),
        )
        return exit_id

    def record_mark(self, tracked: TrackedPosition, value: Decimal, at: dt.datetime) -> None:
        """The mark, and the best and worst marked P&L so far (MFE/MAE for the journal, OP11.2)."""
        position = tracked.position
        pnl = pnl_points(position.structure, position.entry_points, value).quantize(Decimal("0.01"))
        self.conn.execute(
            f"UPDATE {_t(self.schema, 'op_position')} SET last_mark_points = ?, last_mark_at = ?, "
            "peak_value = GREATEST(COALESCE(peak_value, ?), ?), "
            "trough_value = LEAST(COALESCE(trough_value, ?), ?) WHERE session_id = ?",
            (value.quantize(Decimal("0.01")), _aware(at), pnl, pnl, pnl, pnl, tracked.session_id),
        )

    def index_minutes(self, day: dt.date, until: dt.datetime) -> list[Bar]:
        s = self.schema
        start = dt.datetime.combine(day, dt.time(9, 15), tzinfo=IST)
        rows = self.conn.execute(
            "SELECT m.ts, m.open, m.high, m.low, m.close "
            f"FROM {_t(s, 'op_index_minute')} m JOIN {_t(s, 'instrument')} i "
            "ON i.id = m.instrument_id "
            "WHERE i.kite_token = ? AND m.ts >= ? AND m.ts < ? ORDER BY m.ts",
            (NIFTY_50_TOKEN, start, _aware(until)),
        ).fetchall()
        return [
            Bar(
                ts=_aware(r["ts"]).astimezone(IST).replace(tzinfo=None),
                open=Decimal(str(r["open"])),
                high=Decimal(str(r["high"])),
                low=Decimal(str(r["low"])),
                close=Decimal(str(r["close"])),
            )
            for r in rows
        ]


def build_monitor(
    *, enabled: bool, gateway: object | None, store: Any, day: dt.date
) -> NiftyOptionsMonitor | None:
    """The strategy, or ``None`` — and with the flag off, not even constructed (OP9 AC)."""
    if not enabled:
        log.info("BASKFY_OPTIONS_MONITOR_ENABLED is false; the options monitor is not built")
        return None
    return NiftyOptionsMonitor(gateway, store=store, day=day)


def main() -> int:
    """`python -m app.options_monitor` — the session process. Exits 0 and does nothing when the
    flag is off, so a scheduler entry can exist before the flag does."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if not C.OPTIONS_MONITOR_ENABLED:
        log.info("BASKFY_OPTIONS_MONITOR_ENABLED is false; exiting without building the monitor")
        return 0
    from .analytics.db import connect  # noqa: PLC0415 - the DB is only needed with the flag on
    from .core.ticker import TickBus, start_ticker  # noqa: PLC0415
    from .kite_client import Kite  # noqa: PLC0415
    from .options_clock import LegQuotes, run_session  # noqa: PLC0415

    user_id = int(os.environ.get("BASKFY_SOLE_USER_ID", "0") or 0)
    if not user_id:
        log.error("BASKFY_SOLE_USER_ID is not set; op_ rows are keyed by user")
        return 2
    day = dt.datetime.now(tz=IST).date()
    kite = Kite()
    with connect() as conn:
        store = PgPositionStore(conn, user_id=user_id)
        # No gateway at all: the monitor decides exits, OP10's executor sends them.
        strategy = build_monitor(
            enabled=C.OPTIONS_MONITOR_ENABLED, gateway=None, store=store, day=day
        )
        if strategy is None:
            return 0

        async def _serve() -> int:
            bus = TickBus()
            kws = start_ticker(C.KITE_API_KEY, kite.kc.access_token, [NIFTY_50_TOKEN], bus)

            def follow(tokens: list[int]) -> None:
                kws.subscribe(tokens)
                kws.set_mode(kws.MODE_FULL, tokens)

            from . import options_desk, options_execute, options_ledger  # noqa: PLC0415 - lazily
            book = options_execute.PgOptionsStore(conn, user_id=user_id)
            # 09:00 (OP11): the ledger's rules before the open, so a pause a late close earned stands.
            options_ledger.morning_ledger(
                book, now=dt.datetime.now(tz=IST), mode_of=options_desk.mode_of
            )

            async def sweep(_now: dt.datetime) -> None:
                # The monitor raised these exits; the executor closes them under the entry's
                # confirm (PACK.2). The monitor itself never sends anything.
                await options_execute.run_pending_exits(
                    book, options_desk.gateway_for, quotes=options_execute.kite_quotes(kite),
                    mode_of=options_desk.mode_of,
                )

            return await run_session(
                strategy, bus, follow=follow, quotes=LegQuotes(kite), act=sweep
            )

        raised = asyncio.run(_serve())
        log.info("options monitor done: %d exit(s) raised", raised)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
