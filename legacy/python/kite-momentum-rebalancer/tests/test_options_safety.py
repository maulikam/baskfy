"""OP13 — the desk's half of the safety proof (`docs/options/06` OP13, `02` Tracks B and C).

* **The side door** (`02` Track B, OP0.5): with `OPTIONS_ENABLED=true` the frozen lab's `/options*`
  routes, its nav entry, the five `/ops` controls and the autorun's options loop stay dark while
  `frozen/` is not thawed — gated in the live tree by `app.options_lab.lab_enabled()`, not by
  touching `frozen/` (DECISIONS-OP OP13.1) (`TestTheSideDoor`);
* **never overnight**: with every product switch on and `DRY_RUN=false`, the real gateway refuses an
  NRML or CNC option before the broker sees anything (`TestNeverOvernight`);
* **the four-flag AND with a gateway spy**: in each of the fifteen non-LIVE combinations per sleeve a
  leg sent through that sleeve's gateway never reaches the broker; with the sleeve's execution flag
  true but `OPTIONS_ENABLED` false the gateway itself refuses every leg (`TestTheFourFlags`);
* **no second entry per sleeve per day** (`TestNoSecondEntry`, on `baskfy_test`).

The pure properties (never naked over fill paths, exits closing exactly the position) are
`packages/core/tests/test_options_safety_proof.py`; the source scans are there too.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import itertools
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_options_execute import (  # noqa: E402 - the sibling harness
    CONDOR,
    DAY,
    SPREAD,
    conn,  # noqa: F401 - fixture
    confirm,
    deep,
    gateway,
    requires_db,
    seed,
    user,  # noqa: F401 - fixture
)
from test_vbt_safety import SpyingKC  # noqa: E402

from app import config as C  # noqa: E402
from app import options_lab  # noqa: E402
from app.core.gateway import OrderGateway  # noqa: E402
from app.core.risk import RiskManager  # noqa: E402
from app.options_execute import PgOptionsStore, Refused  # noqa: E402
from app.options_gates import options_gates, product_gates  # noqa: E402
from baskfy_core.options.config import Mode, Sleeve  # noqa: E402
from baskfy_execution.gateway import ProductGates  # noqa: E402

SYMBOL = "NIFTY26OCT25150CE"


def _place(gw: OrderGateway, *, product: str = "MIS", symbol: str = SYMBOL) -> dict:
    async def go() -> dict:
        return await gw.place(
            symbol=symbol, qty=65, side="SELL", product=product, exchange="NFO",
            order_type="LIMIT", price=20.0, client_id=f"op13:{product}:{symbol}",
            gross_exposure=1300.0, reference_price=20.0,
        )  # fmt: skip

    return asyncio.run(go())


def _gw(tmp_path: Path, broker: SpyingKC, gates: Any) -> OrderGateway:
    return OrderGateway(broker, RiskManager(), gates=gates,
                        journal_path=str(tmp_path / "op13.jsonl"))  # fmt: skip


class TestTheSideDoor:
    @pytest.fixture(autouse=True)
    def flag_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(C, "OPTIONS_ENABLED", True)

    def test_the_lab_is_frozen_so_the_flag_alone_wakes_nothing(self) -> None:
        assert options_lab.lab_present() is False
        assert options_lab.lab_enabled() is False

    def test_ops_offers_no_strangle_control(self) -> None:
        from app.analytics import ops  # noqa: PLC0415

        assert ops._options_operations() == ()
        assert "Options" not in ops.groups()
        assert not [o for o in ops.all_operations() if o.name.startswith("strangle")]

    def test_the_autorun_never_reaches_the_options_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.analytics import autorun as AR  # noqa: PLC0415
        from app.analytics import db  # noqa: PLC0415

        def woke(*_: object, **__: object) -> list[dict]:
            raise AssertionError("the options loop ran with the lab frozen")

        monkeypatch.setattr(AR, "_underlyings", woke)
        with db.connect(str(tmp_path / "p.db")) as c:
            db.migrate(c)
            items = AR.needed(c, now=dt.datetime(2026, 10, 27, 10, 0), is_trading_day=True)
        assert not [i for i in items if str(i["op"]).startswith("strangle")]

    def test_the_lab_routes_404_and_the_nav_has_no_link(self) -> None:
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from app.main import app, templates  # noqa: PLC0415

        client = TestClient(app)
        assert client.get("/options").status_code == 404
        assert client.get("/options/data").status_code == 404
        assert client.post("/options/run").status_code in (404, 422)
        assert templates.env.globals["options_enabled"] is False
        assert 'href="/options"' not in client.get("/ops").text


class TestNeverOvernight:
    """`02` Track C §1 as an integration test: the guard itself, every switch on."""

    ALL_ON = ProductGates(dry_run=False, intraday_enabled=True, options_enabled=True)

    @pytest.mark.parametrize("product", ["NRML", "CNC"])
    def test_a_carry_product_on_an_option_is_refused_before_the_broker(
        self, tmp_path: Path, product: str
    ) -> None:
        broker = SpyingKC()
        result = _place(_gw(tmp_path, broker, lambda: self.ALL_ON), product=product)
        assert result.get("status") != "DRY_RUN" and result.get("status") != "PLACED"
        assert broker.calls == []

    def test_mis_is_the_one_product_the_same_gateway_would_send(self, tmp_path: Path) -> None:
        """The control: the refusal above is the product, not a broken gateway."""
        broker = SpyingKC()
        _place(_gw(tmp_path, broker, lambda: self.ALL_ON), product="MIS")
        assert broker.calls == ["place_order"]  # a spy, never a broker


class TestTheFourFlags:
    FLAGS = ("DRY_RUN", "OPTIONS_ENABLED", "INTRADAY_ENABLED")
    EXECUTION = {
        Sleeve.O1M: "OPTIONS_O1M_EXECUTION_ENABLED", Sleeve.O1W: "OPTIONS_O1W_EXECUTION_ENABLED",
        Sleeve.O2: "OPTIONS_O2_EXECUTION_ENABLED", Sleeve.O3A: "OPTIONS_O3_EXECUTION_ENABLED",
        Sleeve.O3B: "OPTIONS_O3_EXECUTION_ENABLED",
    }  # fmt: skip

    @pytest.mark.parametrize("sleeve", list(Sleeve))
    def test_fifteen_of_sixteen_never_reach_the_broker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeve: Sleeve
    ) -> None:
        for live_dry, opts, intraday, execution in itertools.product([False, True], repeat=4):
            dry_run = not live_dry
            monkeypatch.setattr(C, "DRY_RUN", dry_run)
            monkeypatch.setattr(C, "OPTIONS_ENABLED", opts)
            monkeypatch.setattr(C, "INTRADAY_ENABLED", intraday)
            monkeypatch.setattr(C, self.EXECUTION[sleeve], execution)
            live = options_gates(sleeve).mode is Mode.LIVE
            assert live == (not dry_run and opts and intraday and execution)
            if live:
                continue  # the one LIVE row: execute_entry refuses it (TestLiveRefused)
            broker = SpyingKC()
            _place(_gw(tmp_path, broker, lambda: product_gates(sleeve)))
            assert broker.calls == [], (sleeve, dry_run, opts, intraday, execution)

    @pytest.mark.parametrize("sleeve", list(Sleeve))
    def test_a_half_flipped_sleeve_is_refused_by_the_gateway_leg_by_leg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeve: Sleeve
    ) -> None:
        monkeypatch.setattr(C, "DRY_RUN", False)
        monkeypatch.setattr(C, "OPTIONS_ENABLED", False)
        monkeypatch.setattr(C, "INTRADAY_ENABLED", True)
        monkeypatch.setattr(C, self.EXECUTION[sleeve], True)
        broker = SpyingKC()
        gw = _gw(tmp_path, broker, lambda: product_gates(sleeve))
        for _role, strike, kind, _side, _token in CONDOR:
            result = _place(gw, symbol=f"NIFTY26OCT{strike}{kind}")
            assert result.get("status") not in ("DRY_RUN", "PLACED")
            assert "OPTIONS_ENABLED" in str(result.get("error", ""))
        assert broker.calls == []


@requires_db
class TestLiveRefused:
    def test_all_four_flags_on_is_refused_before_any_order(
        self, conn: Any, user: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
    ) -> None:
        for name in ("OPTIONS_ENABLED", "INTRADAY_ENABLED", "OPTIONS_O2_EXECUTION_ENABLED"):
            monkeypatch.setattr(C, name, True)
        monkeypatch.setattr(C, "DRY_RUN", False)
        assert options_gates(Sleeve.O2).mode is Mode.LIVE
        plan_id = seed(conn, user, "O2", "LONG_OPTION", SPREAD[:1])
        broker = SpyingKC()
        with pytest.raises(Refused) as caught:
            confirm(PgOptionsStore(conn, user_id=user), gateway(tmp_path, Sleeve.O2, broker),
                    plan_id)  # fmt: skip
        assert caught.value.code == "LIVE_NOT_BUILT"
        assert broker.calls == []


@requires_db
class TestNoSecondEntry:
    def test_a_second_entry_plan_in_a_confirmed_session_is_refused(
        self, conn: Any, user: int, tmp_path: Path  # noqa: F811
    ) -> None:
        plan_id = seed(conn, user, "O3B", "DEBIT_SPREAD", SPREAD)
        store = PgOptionsStore(conn, user_id=user)
        gw = gateway(tmp_path, Sleeve.O3B, SpyingKC())
        assert confirm(store, gw, plan_id).outcome == "OPEN"
        session = conn.execute(
            "SELECT id FROM op_session WHERE plan_id = ?", (plan_id,)
        ).fetchone()["id"]
        # A second ISSUED entry plan hung on the same (now OPEN) session.
        again = f"{plan_id}-again"
        conn.execute(
            "INSERT INTO op_plan (user_id, plan_id, session_id, sleeve, structure, kind, "
            "sizing_mode, issued_at, expires_at, lots, lot_size, risk_budget_inr, status, detail) "
            "SELECT user_id, ?, session_id, sleeve, structure, kind, sizing_mode, issued_at, "
            "expires_at, lots, lot_size, risk_budget_inr, 'ISSUED', detail FROM op_plan "
            "WHERE plan_id = ?",
            (again, plan_id),
        )
        with pytest.raises(Refused) as caught:
            confirm(store, gw, again)
        assert (caught.value.status, caught.value.code) == (409, "NOT_ISSUED")
        assert conn.execute(
            "SELECT count(*) AS n FROM op_order o JOIN op_leg l ON l.id = o.leg_id "
            "JOIN op_plan p ON p.id = l.plan_id WHERE p.session_id = ?", (session,),
        ).fetchone()["n"] == 2  # the first entry's two legs, nothing more

    def test_a_second_session_for_the_sleeve_and_day_cannot_exist(
        self, conn: Any, user: int  # noqa: F811
    ) -> None:
        seed(conn, user, "O2", "LONG_OPTION", SPREAD[:1])
        with pytest.raises(Exception, match="uq_op_session_user_sleeve_date"):
            conn.execute(
                "INSERT INTO op_session (user_id, sleeve, trade_date, mode, state) "
                "VALUES (?, 'O2', ?, 'PAPER', 'PLANNED')",
                (user, DAY),
            )
