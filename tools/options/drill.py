#!/usr/bin/env python
"""A whole paper day per options sleeve through the desk's real path, against Postgres (OP13).

    cd kite-momentum-rebalancer
    BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test \\
        .venv/bin/python ../tools/options/drill.py

`docs/options/06` OP13: "`tools/options/drill.py`: a full paper day per sleeve against Postgres, and
a skip day. AC: the drill prints `sleeve=O1M confirms=1 fills=8 orders_to_broker=0` (and the O2/O3
equivalents) and the journal rows."

WHAT A DAY IS. The OP4 fixture days (``packages/core/tests/options_scan_fixtures.py``) with the
collector's fixture chain at every minute (carry forward; OP6's expiry-day skew for the condors,
OP7/OP8's flat 14 % for O2 and O3):

1. the plan builder decides the day exactly as the worker would (``replay_day.decide_plan`` —
   the same ``decide_o1`` / ``decide_o2`` / ``decide_o3`` the Beat tasks call), and the plan is
   written as the worker writes it: ``op_session`` PLANNED, one ENTRY ``op_plan`` ISSUED, its
   ``op_leg`` rows in send order;
2. the confirm is ``options_execute.execute_entry`` — the handler behind ``POST
   /nifty-options/execute`` — with ``confirm=true``, through the desk's real ``OrderGateway`` built
   with the sleeve's ``product_gates``, over a broker that counts every call it receives;
3. every minute after the entry, ``exits.evaluate`` reads the open position the way the monitor
   does (``PgPositionStore.open_positions``) at that minute's quotes; the first verdict is raised
   as an EXIT plan (``raise_exit``) and closed by ``run_pending_exits`` — the monitor loop's own
   sweep — whose FLAT close writes ``op_journal`` (``options_ledger.after_close``);
4. the skip day is O1-W on the trending Tuesday: the gate says ``NOT_CONTAINED``; nothing is sent.

SAFETY. ``DRY_RUN`` is forced true in this process and every execution flag false; the broker is a
counter that refuses to be a broker (``place_order`` returns an id and records the call — the drill
fails if it was called at all). The database must be a test database (its name contains ``test``)
unless ``--allow-any-db``. Each sleeve runs under a throwaway ``app_user``, deleted at the end with
everything it owns unless ``--keep``.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DESK = ROOT / "kite-momentum-rebalancer"
CORE_TESTS = ROOT / "decile-blueprint" / "packages" / "core" / "tests"
for path in (DESK, CORE_TESTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import options_scan_fixtures as F  # noqa: E402
from app import config as C  # noqa: E402
from app.core.gateway import OrderGateway  # noqa: E402
from app.core.risk import RiskManager  # noqa: E402
from app.options_execute import (  # noqa: E402
    PgOptionsStore,
    Quote,
    execute_entry,
    run_pending_exits,
)
from app.options_gates import options_gates, product_gates  # noqa: E402
from app.options_monitor import PgPositionStore  # noqa: E402

from baskfy_core.options.bars import Bar, closed  # noqa: E402
from baskfy_core.options.config import OptionsCeilings, OptionsConfig, Sleeve  # noqa: E402
from baskfy_core.options.exits import IndexState, LegMark, evaluate  # noqa: E402
from baskfy_core.options.replay_day import Decided, decide_plan  # noqa: E402
from baskfy_core.options.scan import DayContext, Snapshot  # noqa: E402

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
OPTIONS = OptionsConfig()
CEILINGS = OptionsCeilings()


class CountingBroker:
    """Stands where Kite would. Every call is recorded; the drill's verdict is that there were
    none."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        def record(*_: object, **__: object) -> str:
            self.calls.append(name)
            return "DRILL-NEVER"

        return record


def quiet_weekly_bars() -> tuple[Bar, ...]:
    """The quiet monthly's path on the weekly Tuesday: a contained day for O1-W."""
    closes = [b.close for b in F.quiet_monthly_bars()]
    return F.bars_from_closes(F.TREND_WEEKLY, F.quiet_monthly_bars()[0].open, closes)


DAYS: list[tuple[Sleeve, dt.date, tuple[Bar, ...]]] = [
    (Sleeve.O1M, F.QUIET_MONTHLY, F.quiet_monthly_bars()),
    (Sleeve.O1W, F.TREND_WEEKLY, quiet_weekly_bars()),
    (Sleeve.O2, F.O2_UP_BREAK, F.o2_up_break_bars()),
    (Sleeve.O3A, F.TREND_WEEKLY, F.trend_weekly_bars()),
    (Sleeve.O3B, F.GAP_HOLD, F.gap_hold_bars()),
]
SKIP_DAY = (Sleeve.O1W, F.TREND_WEEKLY, F.trend_weekly_bars())


class FixtureChain:
    """The collector's fixture minute at every minute of the day."""

    def __init__(self, day: dt.date, bars: tuple[Bar, ...], smile: Any) -> None:
        self.day, self.bars, self.smile = day, bars, smile
        self._cache: dict[dt.datetime, Snapshot | None] = {}

    def at(self, minute: dt.datetime) -> Snapshot | None:
        key = minute.astimezone(IST).replace(second=0, microsecond=0)
        if key not in self._cache:
            seen = [b for b in self.bars if b.ts + dt.timedelta(minutes=1) <= key]
            if not seen:
                self._cache[key] = None
            else:
                spot = seen[-1].close
                exps = sorted({c.expiry for c in F.MASTER if c.expiry >= self.day})[:2]
                fwd = {e: F.carry_forward(spot, key, e) for e in exps}
                self._cache[key] = F.snapshot(key, spot, exps, self.smile, forwards=fwd)
        return self._cache[key]


def _quotes(snap: Snapshot) -> dict[int, Quote]:
    return {q.instrument_token: Quote(q.bid, q.ask, tuple(q.bids), tuple(q.asks))
            for q in snap.quotes}  # fmt: skip


def _pg_url(explicit: str | None) -> str:
    url = explicit or os.environ.get("BASKFY_TEST_DATABASE_URL") or ""
    if not url:
        raise SystemExit("set BASKFY_TEST_DATABASE_URL or pass --database-url")
    return url.replace("postgresql+asyncpg://", "postgresql://")


def _seed_plan(conn: Any, uid: int, sleeve: Sleeve, day: dt.date, decided: Decided) -> str:
    """The rows the worker's plan builder writes for a decided plan (OP6-OP8)."""
    c = decided.candidate
    for leg in decided.legs:
        conn.execute(
            "INSERT INTO op_contract (instrument_token, tradingsymbol, underlying, expiry, strike, "
            "option_type, lot_size, tick_size, first_seen, last_seen, expired) "
            "SELECT ?, ?, 'NIFTY', ?, ?, ?, ?, 0.05, ?, ?, false "
            "ON CONFLICT (instrument_token) DO NOTHING",
            (leg.instrument_token, leg.tradingsymbol,
             next(k.expiry for k in F.MASTER if k.instrument_token == leg.instrument_token),
             leg.strike, leg.option_type.value, c.lot_size, day, day),
        )  # fmt: skip
    issued = decided.decision_minute + dt.timedelta(seconds=45)
    plan_id = f"{sleeve.value}-{day:%Y%m%d}-drill-{uuid.uuid4().hex[:8]}"
    sid = conn.execute(
        "INSERT INTO op_session (user_id, sleeve, trade_date, expiry_used, mode, state, plan_id) "
        "VALUES (?, ?, ?, ?, 'PAPER', 'PLANNED', ?) RETURNING id",
        (uid, sleeve.value, day, day, plan_id),
    ).fetchone()["id"]
    gate: dict[str, str] = {}
    if decided.range_high is not None and decided.range_low is not None:
        keys = ("or_high", "or_low") if sleeve is Sleeve.O2 else ("range_high", "range_low")
        gate = {keys[0]: str(decided.range_high), keys[1]: str(decided.range_low)}
    if decided.half_gap is not None:
        gate["half_gap"] = str(decided.half_gap)
    detail = {"direction": decided.direction.value if decided.direction else None, "gate": gate}
    width = OPTIONS.expiry_setups.width_points if sleeve in (Sleeve.O3A, Sleeve.O3B) else None
    pk = conn.execute(
        "INSERT INTO op_plan (user_id, plan_id, session_id, sleeve, structure, kind, sizing_mode, "
        "issued_at, expires_at, lots, lot_size, risk_budget_inr, width_points, status, detail) "
        "VALUES (?, ?, ?, ?, ?, 'ENTRY', 'PAPER_ONE_LOT', ?, ?, ?, ?, ?, ?, 'ISSUED', ?) "
        "RETURNING id",
        (uid, plan_id, sid, sleeve.value, c.structure.value, issued,
         issued + dt.timedelta(minutes=30), c.lots, c.lot_size,
         (c.risk_per_lot_inr or Decimal(0)) * c.lots, width, json.dumps(detail)),
    ).fetchone()["id"]  # fmt: skip
    for leg in decided.legs:
        conn.execute(
            "INSERT INTO op_leg (user_id, plan_id, seq, role, tradingsymbol, instrument_token, "
            "strike, option_type, side, quantity) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (uid, pk, leg.seq, leg.role.value, leg.tradingsymbol, leg.instrument_token,
             leg.strike, leg.option_type.value, leg.side.value, leg.quantity),
        )  # fmt: skip
    return plan_id


def _count(conn: Any, sql: str, uid: int) -> int:
    return int(conn.execute(sql, (uid,)).fetchone()["n"])


def run_day(conn: Any, sleeve: Sleeve, day: dt.date, bars: tuple[Bar, ...], tmp: Path) -> dict:
    market = F.market(day, bars)
    # OP6's condor days price an expiry-day skew; the directional days OP7/OP8's flat 14 %.
    smile = F.skew(0.30, 0.33) if sleeve in (Sleeve.O1M, Sleeve.O1W) else F.flat(0.14)
    chain = FixtureChain(day, bars, smile)
    uid = int(conn.execute(
        "INSERT INTO app_user (public_id, email) VALUES (?, ?) RETURNING id",
        (f"drill-{uuid.uuid4().hex[:8]}", f"drill-{uuid.uuid4().hex[:8]}@drill.invalid"),
    ).fetchone()["id"])  # fmt: skip
    broker = CountingBroker()
    out: dict[str, Any] = {"sleeve": sleeve.value, "day": day.isoformat(), "user_id": uid}
    decided = decide_plan(sleeve, market, DayContext(), chain, OPTIONS, CEILINGS)
    if not isinstance(decided, Decided):
        out.update(skipped=decided or "NO_SESSION", confirms=0, fills=0, broker=broker)
        conn.execute(
            "INSERT INTO op_session (user_id, sleeve, trade_date, mode, state, skip_reasons) "
            "VALUES (?, ?, ?, 'PAPER', 'SKIPPED', ?)",
            (uid, sleeve.value, day, [str(decided)]),
        )
        return out
    plan_id = _seed_plan(conn, uid, sleeve, day, decided)
    gateway = OrderGateway(broker, RiskManager(), gates=lambda: product_gates(sleeve),
                           journal_path=str(tmp / f"drill-{sleeve.value}.jsonl"))  # fmt: skip
    store = PgOptionsStore(conn, user_id=uid)
    entry_at = decided.decision_minute + dt.timedelta(minutes=1, seconds=50)
    entry_snap = chain.at(entry_at)
    assert entry_snap is not None
    book = _quotes(entry_snap)
    opened = asyncio.run(execute_entry(
        store, gateway, quotes=lambda token: book[token], plan_id=plan_id, confirm=True,
        mode_of=lambda s: options_gates(s).mode, now=lambda: entry_at, options=OPTIONS,
    ))  # fmt: skip
    out["entry"] = opened.outcome
    positions = PgPositionStore(conn, user_id=uid)
    minute = entry_at.replace(second=0) + dt.timedelta(minutes=1)
    last = dt.datetime.combine(day, dt.time(15, 29), IST)
    while opened.outcome == "OPEN" and minute <= last:
        snap = chain.at(minute)
        tracked = positions.open_positions(day)
        if not tracked:
            break
        if snap is not None:
            q = {x.instrument_token: x for x in snap.quotes}
            (position,) = tracked
            marks = {
                lg.role: LegMark(q[lg.instrument_token].bid, q[lg.instrument_token].ask, minute)
                for lg in position.position.legs if lg.instrument_token in q
            }  # fmt: skip
            seen = closed(market.bars, minute)
            index = IndexState(spot=seen[-1].close if seen else None, last_tick_at=minute,
                               bars=seen)  # fmt: skip
            now = minute + dt.timedelta(seconds=1)
            verdict = evaluate(position.position, marks, index, now, options=OPTIONS)
            if verdict is not None:
                positions.raise_exit(position, verdict, now)
                book_now = _quotes(snap)
                asyncio.run(run_pending_exits(
                    store, lambda _s: gateway,
                    quotes=lambda token, book_now=book_now: book_now[token],
                    now=lambda at=now: at,
                ))  # fmt: skip
                out["exit"] = f"{verdict.code} at {minute:%H:%M}"
                break
        minute += dt.timedelta(minutes=1)
    out["confirms"] = _count(conn, "SELECT count(*) AS n FROM op_plan WHERE user_id = ? AND "
                                   "kind = 'ENTRY' AND status = 'CONFIRMED'", uid)  # fmt: skip
    out["fills"] = _count(conn, "SELECT count(*) AS n FROM op_fill WHERE user_id = ?", uid)
    journal = conn.execute(
        "SELECT sleeve, trade_date, structure, entry_inr, exit_inr, gross_pnl_inr, costs_inr, "
        "net_pnl_inr, r_multiple, closed_reason, minutes_held, simulated, sizing_mode "
        "FROM op_journal WHERE user_id = ?", (uid,),
    ).fetchall()  # fmt: skip
    out["journal"] = [{k: str(v) for k, v in dict(row).items()} for row in journal]
    out["broker"] = broker
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database-url")
    parser.add_argument("--allow-any-db", action="store_true")
    parser.add_argument("--keep", action="store_true", help="keep the drill's rows")
    args = parser.parse_args(argv)
    url = _pg_url(args.database_url)
    if "test" not in url.rsplit("/", 1)[-1] and not args.allow_any_db:
        name = url.rsplit("/", 1)[-1]
        raise SystemExit(f"refusing a database whose name has no 'test' in it: {name}")
    C.DRY_RUN = True
    for name in ("OPTIONS_O1M_EXECUTION_ENABLED", "OPTIONS_O1W_EXECUTION_ENABLED",
                 "OPTIONS_O2_EXECUTION_ENABLED", "OPTIONS_O3_EXECUTION_ENABLED"):
        setattr(C, name, False)
    from app.analytics.pg import Connection  # noqa: PLC0415 - psycopg only here

    conn = Connection(url)
    tmp = Path(os.environ.get("TMPDIR", "/tmp"))
    results: list[dict] = []
    failed = False
    try:
        for sleeve, day, bars in [*DAYS, SKIP_DAY]:
            result = run_day(conn, sleeve, day, bars, tmp)
            results.append(result)
            calls = len(result["broker"].calls)
            failed |= calls != 0
            head = (f"sleeve={result['sleeve']} confirms={result['confirms']} "
                    f"fills={result['fills']} orders_to_broker={calls} "
                    f"day={result['day']}")  # fmt: skip
            if "skipped" in result:
                print(f"{head} skipped={result['skipped']}")
                continue
            print(f"{head} entry={result['entry']} exit={result.get('exit', '-')}")
            for row in result["journal"]:
                print("  journal " + " ".join(f"{k}={v}" for k, v in row.items()))
            failed |= not result["journal"]
    finally:
        if not args.keep:
            for result in results:
                conn.execute("DELETE FROM app_user WHERE id = ?", (result["user_id"],))
        conn.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
