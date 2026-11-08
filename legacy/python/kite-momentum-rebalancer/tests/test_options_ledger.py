"""OP11 — the journal and the risk ledger on the desk (`docs/options/06` OP11).

`06` OP11's desk acceptance criteria, on a real PostgreSQL through the real paper path:

* every close writes `op_journal` with the cost breakdown, R and MAE/MFE (`TestTheJournal`);
* −2R intraday on O2 → `DAILY_R`, the position closed, the sleeve paused today (`TestDailyR`);
* four −2R O1-W weeks → `MONTHLY_R` (not `WEEKLY_R`) — `06` wrote "four −1R O1-W weeks", which is
  −4R and cannot reach `04` §9.1's 8R month (DECISIONS-OP OP11.1) (`TestMonthlyR`);
* a book ₹ breach closes every open position (`TestTheBook`).

"Five real rows lift `half_size`" and "no summary pools sleeves, simulated or sizing modes" are the
worker's (`services/worker/tests/test_options_weekly_task.py`).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_options_execute import (  # noqa: E402 - the sibling harness
    CONDOR,
    DAY,
    NOW,
    SPREAD,
    SpyingKC,
    book,
    confirm,
    conn,  # noqa: F401 - fixture
    deep,
    gateway,
    requires_db,
    seed,
    user,  # noqa: F401 - fixture
)

from app import options_execute, options_ledger  # noqa: E402
from app.options_execute import PgOptionsStore  # noqa: E402
from app.options_monitor import PgPositionStore  # noqa: E402
from baskfy_core.options.config import Mode, OptionsConfig, Side, Sleeve  # noqa: E402
from baskfy_core.options.costs import CostFill, charges  # noqa: E402
from baskfy_core.options.exits import ExitVerdict  # noqa: E402

LATER = NOW + dt.timedelta(hours=1)


def close_all(store: PgOptionsStore, gw: Any, quotes: dict) -> list[options_execute.ExecOutcome]:
    async def go() -> list[options_execute.ExecOutcome]:
        return await options_execute.run_pending_exits(
            store, lambda s: gw, quotes=lambda t: quotes[t], now=lambda: LATER
        )

    return asyncio.run(go())


def raise_exit(conn: Any, user: int, sleeve: Sleeve, code: str = "STOP") -> None:
    positions = PgPositionStore(conn, user_id=user)
    for tracked in positions.open_positions(DAY):
        if tracked.position.sleeve is sleeve:
            positions.raise_exit(tracked, ExitVerdict(code, "RULE", None, None, False), LATER)


def session_of(conn: Any, plan_id: str) -> Any:
    return conn.execute("SELECT * FROM op_session WHERE plan_id = ?", (plan_id,)).fetchone()


@requires_db
class TestTheJournal:
    def test_a_close_writes_the_row_with_costs_r_and_mae_mfe(
        self, conn: Any, user: int, tmp_path: Path
    ) -> None:
        plan_id = seed(conn, user, "O1M", "IRON_CONDOR", CONDOR)
        store = PgOptionsStore(conn, user_id=user)
        gw = gateway(tmp_path, Sleeve.O1M, SpyingKC())
        confirm(store, gw, plan_id)
        positions = PgPositionStore(conn, user_id=user)
        (tracked,) = positions.open_positions(DAY)
        # Credit 29.80; marks at a 35.00 cost (−5.20) and a 10.00 cost (+19.80): the extremes.
        positions.record_mark(tracked, Decimal("35.00"), NOW + dt.timedelta(minutes=5))
        positions.record_mark(tracked, Decimal("10.00"), NOW + dt.timedelta(minutes=30))
        raise_exit(conn, user, Sleeve.O1M, "PROFIT")
        exit_book = deep() | {9303: book("4.90", "5.00"), 9304: book("4.90", "5.00"),
                              9301: book("0.50", "0.60"), 9302: book("0.50", "0.60")}  # fmt: skip
        (out,) = close_all(store, gw, exit_book)
        assert out.outcome == "FLAT"
        row = conn.execute(
            "SELECT * FROM op_journal WHERE session_id = ?", (session_of(conn, plan_id)["id"],)
        ).fetchone()
        assert row is not None and row["closed_reason"] == "PROFIT" and row["simulated"]
        # Entry: shorts sold at 20.00, wings bought at 5.10; exit: shorts bought at 5.00, wings
        # sold at 0.50 — the ladder's resting prices (04 §8.4).
        fills = [
            CostFill(Side.BUY, Decimal("5.10"), 65), CostFill(Side.BUY, Decimal("5.10"), 65),
            CostFill(Side.SELL, Decimal("20.00"), 65), CostFill(Side.SELL, Decimal("20.00"), 65),
            CostFill(Side.BUY, Decimal("5.00"), 65), CostFill(Side.BUY, Decimal("5.00"), 65),
            CostFill(Side.SELL, Decimal("0.50"), 65), CostFill(Side.SELL, Decimal("0.50"), 65),
        ]  # fmt: skip
        cost = charges(fills, OptionsConfig().costs)
        gross = (Decimal("29.80") - Decimal("9.00")) * 65  # credit less the cost to close
        assert Decimal(str(row["gross_pnl_inr"])) == gross
        assert Decimal(str(row["costs_inr"])) == cost.total
        assert Decimal(str(row["net_pnl_inr"])) == gross - cost.total
        assert Decimal(str(row["risk_budget_inr"])) == Decimal("2500.00")
        assert Decimal(str(row["r_multiple"])) == ((gross - cost.total) / 2500).quantize(Decimal("0.01"))
        assert Decimal(str(row["mfe_r"])) == (Decimal("19.80") * 65 / 2500).quantize(Decimal("0.01"))
        assert Decimal(str(row["mae_r"])) == (Decimal("-5.20") * 65 / 2500).quantize(Decimal("0.01"))
        assert row["detail"]["costs"]["orders"] == 8


@requires_db
class TestDailyR:
    def test_minus_2r_on_o2_pauses_the_sleeve_today(
        self, conn: Any, user: int, tmp_path: Path
    ) -> None:
        plan_id = seed(conn, user, "O2", "LONG_OPTION", SPREAD[:1])
        conn.execute(
            "UPDATE op_plan SET risk_budget_inr = 500, sizing_mode = 'BUDGET' WHERE plan_id = ?",
            (plan_id,),
        )
        store = PgOptionsStore(conn, user_id=user)
        gw = gateway(tmp_path, Sleeve.O2, SpyingKC())
        confirm(store, gw, plan_id)  # bought at 35.20
        raise_exit(conn, user, Sleeve.O2, "STOP")
        (out,) = close_all(store, gw, deep() | {9311: book("20.00", "20.20")})  # sold at 20.00
        assert out.outcome == "FLAT" and "DAILY_R" in out.detail["pauses"]
        journal = conn.execute(
            "SELECT r_multiple FROM op_journal WHERE session_id = ?", (session_of(conn, plan_id)["id"],)
        ).fetchone()
        assert Decimal(str(journal["r_multiple"])) <= Decimal("-2")  # −(15.20 × 65 + costs) / 500
        config = conn.execute(
            "SELECT paused_until, paused_reason FROM op_sleeve_config WHERE user_id = ? AND sleeve = 'O2'",
            (user,),
        ).fetchone()
        assert (str(config["paused_until"])[:10], config["paused_reason"]) == (DAY.isoformat(), "DAILY_R")
        audit = conn.execute(
            "SELECT changed_by, new_value FROM op_config_audit WHERE user_id = ? AND scope = 'O2'",
            (user,),
        ).fetchall()
        assert [(a["changed_by"], a["new_value"]) for a in audit] == [("ledger", DAY.isoformat())]
        assert session_of(conn, plan_id)["state"] == "CLOSED"


@requires_db
class TestMonthlyR:
    def test_four_minus_2r_o1w_weeks_pause_to_month_end_not_by_week(
        self, conn: Any, user: int
    ) -> None:
        for day in (dt.date(2026, 10, 6), dt.date(2026, 10, 13), dt.date(2026, 10, 20), DAY):
            sid = conn.execute(
                "INSERT INTO op_session (user_id, sleeve, trade_date, mode, state) "
                "VALUES (?, 'O1W', ?, 'PAPER', 'CLOSED') RETURNING id",
                (user, day),
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO op_journal (session_id, user_id, sleeve, trade_date, expiry_used, "
                "structure, entry_inr, exit_inr, gross_pnl_inr, costs_inr, net_pnl_inr, "
                "risk_budget_inr, r_multiple, closed_reason, minutes_held, simulated, sizing_mode) "
                "VALUES (?, ?, 'O1W', ?, ?, 'IRON_CONDOR', 2500, 7500, -5000, 0, -5000, 2500, -2, "
                "'STOP', 60, true, 'PAPER_ONE_LOT')",
                (sid, user, day, day),
            )
        store = PgOptionsStore(conn, user_id=user)
        applied = options_ledger.apply_ledger(
            store, sleeve=Sleeve.O1W, today=DAY, now=NOW, r_today_inr=Decimal("2500"),
            mode=Mode.PAPER,
        )  # fmt: skip
        # Each week is −2R (above the 4R week), the month is −8R: MONTHLY_R alone.
        assert "MONTHLY_R" in applied and "WEEKLY_R" not in applied
        config = conn.execute(
            "SELECT paused_until FROM op_sleeve_config WHERE user_id = ? AND sleeve = 'O1W'", (user,)
        ).fetchone()
        assert str(config["paused_until"])[:10] == "2026-10-31"

    def test_a_pause_is_never_shortened(self, conn: Any, user: int) -> None:
        conn.execute(
            "INSERT INTO op_sleeve_config (user_id, sleeve, paused_until, paused_reason) "
            "VALUES (?, 'O2', '2026-11-30', 'MANUAL')",
            (user,),
        )
        from baskfy_core.options.risk import Pause, PauseReason  # noqa: PLC0415

        store = PgOptionsStore(conn, user_id=user)
        options_ledger._write_pause(store, "O2", Pause(DAY, (PauseReason.DAILY_R,), True))
        config = conn.execute(
            "SELECT paused_until FROM op_sleeve_config WHERE user_id = ? AND sleeve = 'O2'", (user,)
        ).fetchone()
        assert str(config["paused_until"])[:10] == "2026-11-30"


@requires_db
class TestTheBook:
    def test_a_book_rupee_breach_closes_every_open_position(
        self, conn: Any, user: int, tmp_path: Path
    ) -> None:
        # Capital makes the book's limits real: ₹50,000 → the day limit is 1.5 % = ₹750 (04 §9.3).
        conn.execute(
            "INSERT INTO op_sleeve_config (user_id, sleeve, sleeve_capital_inr) VALUES (?, 'O2', 50000)",
            (user,),
        )
        condor = seed(conn, user, "O1M", "IRON_CONDOR", CONDOR)
        o2 = seed(conn, user, "O2", "LONG_OPTION", SPREAD[:1])
        conn.execute(
            "UPDATE op_plan SET risk_budget_inr = 5000, sizing_mode = 'BUDGET' WHERE plan_id = ?", (o2,)
        )
        store = PgOptionsStore(conn, user_id=user)
        gw1, gw2 = gateway(tmp_path, Sleeve.O1M, SpyingKC()), gateway(tmp_path, Sleeve.O2, SpyingKC())
        confirm(store, gw1, condor)
        confirm(store, gw2, o2)
        raise_exit(conn, user, Sleeve.O2, "STOP")
        (out,) = close_all(store, gw2, deep() | {9311: book("20.00", "20.20")})  # −₹988 and costs
        assert "BOOK_DAILY_INR" in out.detail["pauses"]
        # The condor still open got its exit plan from the ledger, and the next sweep closes it.
        exit_plan = conn.execute(
            "SELECT p.exit_plan_id, x.detail FROM op_position p JOIN op_plan x "
            "ON x.plan_id = p.exit_plan_id WHERE p.session_id = ?", (session_of(conn, condor)["id"],),
        ).fetchone()  # fmt: skip
        assert exit_plan is not None and exit_plan["detail"]["code"] == "BOOK_DAILY_INR"
        (closed,) = close_all(store, gw1, deep())
        assert closed.outcome == "FLAT" and session_of(conn, condor)["state"] == "CLOSED"
        book_row = conn.execute(
            "SELECT paused_until, paused_reason FROM op_book_config WHERE user_id = ?", (user,)
        ).fetchone()
        assert book_row["paused_reason"].startswith("BOOK_DAILY_INR")
