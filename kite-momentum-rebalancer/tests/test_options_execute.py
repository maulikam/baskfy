"""OP10 — confirming an options plan on paper, through the real gateway (`docs/options/06` OP10).

`06` OP10's desk acceptance criteria, with `DRY_RUN` and every flag false:

* O1 confirm → four simulated fills wings-first and `OPEN` (`TestTheConfirm`);
* thin long-call depth → `ABANDONED_ENTRY`, no short ever sent; O3 partial long → abandoned, no
  short sent (`TestAbandonment`);
* an exit produces closes in sequence — shorts first — and closes the position (`TestTheExit`; the
  marketable last attempt is `packages/core/tests/test_options_executor.py`'s);
* a gateway spy records **0 broker calls** under both `DRY_RUN` values with the execution flag false
  (`TestNoBrokerCall`);
* never-naked across 500 seeded sequences per structure — the pure executor's
  (`test_options_executor.py::TestNeverNaked`);
* no path calls `place_gtt_stop` (`TestNoGtt`).

Everything runs on a real PostgreSQL with the screener's `op_` schema (`BASKFY_TEST_DATABASE_URL`;
skipped without one) and the desk's real `OrderGateway` over a spy broker.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import json
import os
import re
import sys
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_vbt_safety import SpyingKC  # noqa: E402 - the sibling harness

from app import config as C  # noqa: E402
from app import options_desk, options_execute  # noqa: E402
from app.core.gateway import OrderGateway  # noqa: E402
from app.core.risk import RiskManager  # noqa: E402
from app.options_execute import PgOptionsStore, Quote, Refused, execute_entry  # noqa: E402
from app.options_gates import options_gates, product_gates  # noqa: E402
from app.options_monitor import PgPositionStore  # noqa: E402
from baskfy_core.options.chain import Level  # noqa: E402
from baskfy_core.options.config import Mode, Sleeve  # noqa: E402
from baskfy_core.options.exits import ExitVerdict  # noqa: E402

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DAY = dt.date(2026, 10, 27)
NOW = dt.datetime(2026, 10, 27, 10, 5, tzinfo=IST)
QTY = 65


def _pg_url() -> str | None:
    url = os.environ.get("BASKFY_TEST_DATABASE_URL")
    return None if not url else url.replace("postgresql+asyncpg://", "postgresql://")


requires_db = pytest.mark.skipif(_pg_url() is None, reason="BASKFY_TEST_DATABASE_URL is not set")

CONDOR = [  # role, strike, type, side, token — in the worker's send order (wings first)
    ("LONG_PUT", 24750, "PE", "BUY", 9301),
    ("LONG_CALL", 25250, "CE", "BUY", 9302),
    ("SHORT_PUT", 24850, "PE", "SELL", 9303),
    ("SHORT_CALL", 25150, "CE", "SELL", 9304),
]
SPREAD = [("LONG_CALL", 25200, "CE", "BUY", 9311), ("SHORT_CALL", 25300, "CE", "SELL", 9312)]


def book(bid: str, ask: str, depth: int = 1300) -> Quote:
    b, a = Decimal(bid), Decimal(ask)
    return Quote(b, a, (Level(b, depth),), (Level(a, depth),))


def deep() -> dict[int, Quote]:
    return {9301: book("5.00", "5.10"), 9302: book("5.00", "5.10"), 9303: book("20.00", "20.20"),
            9304: book("20.00", "20.20"), 9311: book("35.00", "35.20"),
            9312: book("5.00", "5.10")}  # fmt: skip


@pytest.fixture
def conn() -> Iterator[Any]:
    url = _pg_url()
    if url is None:
        pytest.skip("BASKFY_TEST_DATABASE_URL is not set")
    from app.analytics.pg import Connection  # noqa: PLC0415 - psycopg only with a database

    c = Connection(url)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def user(conn: Any) -> Iterator[int]:
    uid = int(conn.execute(
        "INSERT INTO app_user (public_id, email) VALUES (?, ?) RETURNING id",
        (f"op10-{uuid.uuid4().hex[:8]}", f"op10-{uuid.uuid4().hex[:8]}@x.test"),
    ).fetchone()["id"])  # fmt: skip
    try:
        yield uid
    finally:
        conn.execute("DELETE FROM app_user WHERE id = ?", (uid,))


def seed(conn: Any, uid: int, sleeve: str, structure: str, legs: list, *, slot: str | None = None,
         expires: dt.datetime | None = None) -> str:  # fmt: skip
    """One PLANNED session, its ISSUED entry plan and its legs — as the worker writes them."""
    for _role, strike, kind, _side, token in legs:
        conn.execute(
            "INSERT INTO op_contract (instrument_token, tradingsymbol, underlying, expiry, strike, "
            "option_type, lot_size, tick_size, first_seen, last_seen, expired) VALUES "
            "(?, ?, 'NIFTY', ?, ?, ?, 65, 0.05, ?, ?, false) ON CONFLICT (instrument_token) DO NOTHING",
            (token, f"OP10{token}{kind}", DAY, strike, kind, DAY, DAY),
        )
    plan_id = f"{sleeve}-20261027-{uuid.uuid4().hex[:12]}"
    sid = conn.execute(
        "INSERT INTO op_session (user_id, sleeve, trade_date, expiry_used, mode, state, plan_id) "
        "VALUES (?, ?, ?, ?, 'PAPER', 'PLANNED', ?) RETURNING id",
        (uid, sleeve, DAY, DAY, plan_id),
    ).fetchone()["id"]
    if slot is not None:  # another sleeve's confirmed session holds the slot
        conn.execute(
            "INSERT INTO op_session (user_id, sleeve, trade_date, mode, state, slot_holder) "
            "VALUES (?, ?, ?, 'PAPER', 'CONFIRMED', ?)",
            (uid, slot, DAY, slot),
        )
    pk = conn.execute(
        "INSERT INTO op_plan (user_id, plan_id, session_id, sleeve, structure, kind, sizing_mode, "
        "issued_at, expires_at, lots, lot_size, risk_budget_inr, status, detail) VALUES "
        "(?, ?, ?, ?, ?, 'ENTRY', 'PAPER_ONE_LOT', ?, ?, 1, 65, 2500, 'ISSUED', ?) RETURNING id",
        (uid, plan_id, sid, sleeve, structure, NOW - dt.timedelta(minutes=1),
         expires or NOW + dt.timedelta(minutes=25),
         json.dumps({"direction": "UP", "gate": {"range_high": "25182", "range_low": "24998"}})),
    ).fetchone()["id"]  # fmt: skip
    for seq, (role, strike, kind, side, token) in enumerate(legs, start=1):
        conn.execute(
            "INSERT INTO op_leg (user_id, plan_id, seq, role, tradingsymbol, instrument_token, strike, "
            "option_type, side, quantity) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 65)",
            (uid, pk, seq, role, f"OP10{token}{kind}", token, strike, kind, side),
        )
    return plan_id


def gateway(tmp_path: Path, sleeve: Sleeve, broker: SpyingKC) -> OrderGateway:
    return OrderGateway(
        broker, RiskManager(), gates=lambda: product_gates(sleeve),
        journal_path=str(tmp_path / "options.jsonl"),
    )  # fmt: skip


def confirm(store: PgOptionsStore, gw: OrderGateway, plan_id: str,
            quotes: dict[int, Quote] | None = None) -> options_execute.ExecOutcome:  # fmt: skip
    q = quotes or deep()

    async def go() -> options_execute.ExecOutcome:
        return await execute_entry(
            store, gw, quotes=lambda token: q[token], plan_id=plan_id, confirm=True,
            mode_of=lambda s: options_gates(s).mode, now=lambda: NOW,
        )  # fmt: skip

    return asyncio.run(go())


def orders(conn: Any, plan_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT l.role, o.side, o.client_id, o.gateway_status, o.simulated, o.quantity "
        "FROM op_order o JOIN op_leg l ON l.id = o.leg_id JOIN op_plan p ON p.id = l.plan_id "
        "WHERE p.plan_id = ? ORDER BY o.id", (plan_id,),
    ).fetchall()]  # fmt: skip


@requires_db
class TestTheConfirm:
    def test_o1_confirm_is_four_simulated_fills_wings_first_and_open(
        self, conn: Any, user: int, tmp_path: Path
    ) -> None:
        plan_id = seed(conn, user, "O1M", "IRON_CONDOR", CONDOR)
        store = PgOptionsStore(conn, user_id=user)
        broker = SpyingKC()
        out = confirm(store, gateway(tmp_path, Sleeve.O1M, broker), plan_id)
        assert (out.outcome, out.session_state, out.simulated) == ("OPEN", "OPEN", True)
        sent = orders(conn, plan_id)
        assert [o["role"] for o in sent[:2]] == ["LONG_PUT", "LONG_CALL"]  # wings first
        assert {o["role"] for o in sent[2:]} == {"SHORT_PUT", "SHORT_CALL"}
        assert all(o["gateway_status"] == "DRY_RUN" and o["simulated"] for o in sent)
        assert [o["client_id"] for o in sent][0] == f"{plan_id}:OP109301PE"
        fills = conn.execute(
            "SELECT count(*) AS n, bool_and(f.simulated) AS sim FROM op_fill f JOIN op_leg l "
            "ON l.id = f.leg_id JOIN op_plan p ON p.id = l.plan_id WHERE p.plan_id = ?",
            (plan_id,),
        ).fetchone()
        assert (fills["n"], fills["sim"]) == (4, True)
        pos = conn.execute(
            "SELECT p.entry_points, p.lots, p.simulated, s.state, s.slot_holder FROM op_position p "
            "JOIN op_session s ON s.id = p.session_id WHERE s.plan_id = ?", (plan_id,),
        ).fetchone()  # fmt: skip
        # Credit from fills: the ladder fills at the resting level inside the limit (04 §8.4) —
        # the shorts at the 20.00 bid, the wings at the 5.10 ask: 2 x 20.00 - 2 x 5.10.
        assert Decimal(str(pos["entry_points"])) == Decimal("29.80")
        assert (pos["state"], pos["slot_holder"], pos["simulated"]) == ("OPEN", "O1M", True)
        assert broker.calls == []

    def test_an_expired_plan_is_refused_with_410(self, conn: Any, user: int, tmp_path: Path) -> None:
        plan_id = seed(conn, user, "O2", "LONG_OPTION", SPREAD[:1], expires=NOW)
        store = PgOptionsStore(conn, user_id=user)
        with pytest.raises(Refused) as caught:
            confirm(store, gateway(tmp_path, Sleeve.O2, SpyingKC()), plan_id)
        assert (caught.value.status, caught.value.code) == (410, "PLAN_EXPIRED")
        assert orders(conn, plan_id) == []

    def test_a_taken_slot_is_refused_with_409(self, conn: Any, user: int, tmp_path: Path) -> None:
        plan_id = seed(conn, user, "O3A", "DEBIT_SPREAD", SPREAD, slot="O1W")
        store = PgOptionsStore(conn, user_id=user)
        with pytest.raises(Refused) as caught:
            confirm(store, gateway(tmp_path, Sleeve.O3A, SpyingKC()), plan_id)
        assert (caught.value.status, caught.value.code) == (409, "SLOT_TAKEN")
        assert orders(conn, plan_id) == []

    def test_a_second_confirm_is_refused(self, conn: Any, user: int, tmp_path: Path) -> None:
        plan_id = seed(conn, user, "O3A", "DEBIT_SPREAD", SPREAD)
        store = PgOptionsStore(conn, user_id=user)
        gw = gateway(tmp_path, Sleeve.O3A, SpyingKC())
        confirm(store, gw, plan_id)
        with pytest.raises(Refused) as caught:
            confirm(store, gw, plan_id)
        assert caught.value.status == 409

    def test_live_is_refused_before_any_order(self, conn: Any, user: int, tmp_path: Path) -> None:
        plan_id = seed(conn, user, "O2", "LONG_OPTION", SPREAD[:1])
        store = PgOptionsStore(conn, user_id=user)

        async def go() -> None:
            await execute_entry(
                store, gateway(tmp_path, Sleeve.O2, SpyingKC()), quotes=lambda t: deep()[t],
                plan_id=plan_id, confirm=True, mode_of=lambda s: Mode.LIVE, now=lambda: NOW,
            )  # fmt: skip

        with pytest.raises(Refused) as caught:
            asyncio.run(go())
        assert caught.value.code == "LIVE_NOT_BUILT"
        assert orders(conn, plan_id) == []


@requires_db
class TestAbandonment:
    def test_thin_long_call_depth_abandons_and_no_short_is_ever_sent(
        self, conn: Any, user: int, tmp_path: Path
    ) -> None:
        plan_id = seed(conn, user, "O1W", "IRON_CONDOR", CONDOR)
        store = PgOptionsStore(conn, user_id=user)
        thin = deep() | {9302: book("5.00", "5.10", depth=0)}  # nothing resting at the long call
        out = confirm(store, gateway(tmp_path, Sleeve.O1W, SpyingKC()), plan_id, thin)
        assert (out.outcome, out.session_state) == ("ABANDONED_ENTRY", "CLOSED")
        sent = orders(conn, plan_id)
        assert not any(o["role"].startswith("SHORT") for o in sent)
        # The long put that filled is sold back (its close is on the ledger), the long call cancelled.
        assert [(o["role"], o["side"]) for o in sent if o["side"] == "SELL"] == [("LONG_PUT", "SELL")]
        session = conn.execute(
            "SELECT state, closed_reason, slot_holder FROM op_session WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        assert (session["state"], session["closed_reason"], session["slot_holder"]) == (
            "CLOSED", "ABANDONED_ENTRY", None,
        )  # fmt: skip

    def test_an_o3_partial_long_is_abandoned_and_no_short_sent(
        self, conn: Any, user: int, tmp_path: Path
    ) -> None:
        plan_id = seed(conn, user, "O3B", "DEBIT_SPREAD", SPREAD)
        store = PgOptionsStore(conn, user_id=user)
        partial = deep() | {9311: book("35.00", "35.20", depth=20)}  # 20 of 65 per attempt
        out = confirm(store, gateway(tmp_path, Sleeve.O3B, SpyingKC()), plan_id, partial)
        assert out.outcome == "ABANDONED_ENTRY"
        assert not any(o["role"] == "SHORT_CALL" for o in orders(conn, plan_id))
        assert store.net_open(int(conn.execute(
            "SELECT id FROM op_session WHERE plan_id = ?", (plan_id,)).fetchone()["id"])) == {
            options_execute.LegRole.LONG_CALL: 0, options_execute.LegRole.SHORT_CALL: 0,
        }  # fmt: skip


@requires_db
class TestTheExit:
    def test_the_monitors_exit_closes_shorts_first_and_the_position(
        self, conn: Any, user: int, tmp_path: Path
    ) -> None:
        plan_id = seed(conn, user, "O1M", "IRON_CONDOR", CONDOR)
        store = PgOptionsStore(conn, user_id=user)
        gw = gateway(tmp_path, Sleeve.O1M, SpyingKC())
        confirm(store, gw, plan_id)
        positions = PgPositionStore(conn, user_id=user)
        (tracked,) = positions.open_positions(DAY)
        exit_id = positions.raise_exit(
            tracked, ExitVerdict("PROFIT", "RULE", Decimal("14.80"), Decimal("-962"), False),
            dt.datetime(2026, 10, 27, 11, 14, 50),
        )  # fmt: skip

        async def go() -> list[options_execute.ExecOutcome]:
            return await options_execute.run_pending_exits(
                store, lambda s: gw, quotes=lambda t: deep()[t], now=lambda: NOW
            )

        (out,) = asyncio.run(go())
        assert (out.outcome, out.session_state) == ("FLAT", "CLOSED")
        closes = [o for o in orders(conn, plan_id) if o["client_id"].split(":")[2:3] == ["CLOSE"]]
        assert [o["role"] for o in closes[:2]] == ["SHORT_CALL", "SHORT_PUT"]
        assert {o["role"] for o in closes[2:]} == {"LONG_CALL", "LONG_PUT"}
        state = conn.execute(
            "SELECT s.state, s.closed_reason, p.closed_at IS NOT NULL AS closed, x.status "
            "FROM op_session s JOIN op_position p ON p.session_id = s.id "
            "JOIN op_plan x ON x.plan_id = ? WHERE s.plan_id = ?", (exit_id, plan_id),
        ).fetchone()  # fmt: skip
        assert (state["state"], state["closed_reason"], state["closed"], state["status"]) == (
            "CLOSED", "PROFIT", True, "CONFIRMED",
        )  # fmt: skip
        # Nothing is left open, and a second sweep finds nothing to do.
        assert asyncio.run(go()) == []


@requires_db
class TestNoBrokerCall:
    @pytest.mark.parametrize("dry_run", [True, False])
    def test_zero_broker_calls_under_both_dry_run_values(
        self, conn: Any, user: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
    ) -> None:
        """The sleeve's execution flag is false: PAPER whatever `DRY_RUN` says (`02` Track B)."""
        monkeypatch.setattr(C, "DRY_RUN", dry_run)
        assert C.OPTIONS_O3_EXECUTION_ENABLED is False
        plan_id = seed(conn, user, "O3A", "DEBIT_SPREAD", SPREAD)
        store = PgOptionsStore(conn, user_id=user)
        broker = SpyingKC()
        out = confirm(store, gateway(tmp_path, Sleeve.O3A, broker), plan_id)
        assert out.outcome == "OPEN"
        assert broker.calls == []
        assert all(o["gateway_status"] == "DRY_RUN" for o in orders(conn, plan_id))


class TestNoGtt:
    @pytest.mark.parametrize("module", [options_execute, options_desk])
    def test_no_path_calls_place_gtt_stop(self, module: object) -> None:
        source = inspect.getsource(module)  # type-agnostic: the module's text
        code = re.sub(r'("""|\'\'\')(?:.|\n)*?\1', "", source)
        assert re.search(r"\bplace_gtt", code) is None
        assert re.search(r"\.place_order\(", code) is None

    def test_every_leg_is_nfo_mis_limit_through_the_gateway(self) -> None:
        code = inspect.getsource(options_execute.DeskVenue.send)
        for part in ('product="MIS"', 'exchange="NFO"', 'order_type="LIMIT"', "self.gateway.place("):
            assert part in code, part

    def test_the_routes_are_exactly_two_posts_and_no_get_executes(self) -> None:
        source = inspect.getsource(options_desk)
        assert len(re.findall(r"@router\.post\(", source)) == 2
        assert not re.search(r'@router\.get\("[^"]*(execute|close)', source)

    def test_the_confirm_sentences_are_05s_verbatim(self) -> None:
        assert options_desk.sentence_for("O1M").startswith(
            "Confirming this plan also authorises its rule-driven exits — profit at ½C, stop at 1.5C"
        )
        assert "the 45-minute time stop, and the flat at 15:00" in options_desk.sentence_for("O2")
        assert options_desk.sentence_for("O3B").endswith("and the flat at 14:45 — without a second click.")


@requires_db
class TestThePage:
    def test_the_page_shows_the_plan_the_button_and_the_sentence(
        self, conn: Any, user: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import contextlib  # noqa: PLC0415

        from fastapi.testclient import TestClient  # noqa: PLC0415

        from app import main as M  # noqa: PLC0415

        plan_id = seed(conn, user, "O3A", "DEBIT_SPREAD", SPREAD)

        @contextlib.contextmanager
        def store() -> Iterator[PgOptionsStore]:
            yield PgOptionsStore(conn, user_id=user)

        monkeypatch.setattr(options_desk, "open_store", store)
        monkeypatch.setattr(options_desk, "_now", lambda: NOW)
        client = TestClient(M.app)
        page = client.get("/nifty-options")
        assert page.status_code == 200
        assert plan_id in page.text
        assert "Confirm — simulated" in page.text
        assert "80 % of width, half the debit lost" in page.text
        data = client.get("/nifty-options/data").json()
        assert data["ok"] and data["plans"][0]["legs"][0]["role"] == "LONG_CALL"

    def test_a_confirm_that_is_not_true_is_400_before_any_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from app import main as M  # noqa: PLC0415

        def boom() -> None:
            raise AssertionError("the store was opened")

        monkeypatch.setattr(options_desk, "open_store", boom)
        client = TestClient(M.app)
        for path, field in (("/nifty-options/execute", "plan_id"), ("/nifty-options/close", "session_id")):
            r = client.post(path, data={field: "1", "confirm": "false"})
            assert r.status_code == 400, (path, r.text)
            assert r.json()["code"] == "CONFIRM_REQUIRED"
