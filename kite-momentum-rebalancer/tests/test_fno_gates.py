"""docs/fno/06 FO6, on the desk: `fno_gates()` is PAPER for every combination of the four
switches but all-four-true, per sleeve, read off `app.config` at the moment of the order.

`product_gates()` is what the FO gateway will be handed (FO8): `dry_run=True` in every PAPER row
and `intraday_enabled=False` in every row. Every desk FO flag defaults false, and no FO
auto-execute setting exists.
"""
from __future__ import annotations

import asyncio
import itertools
import re
from pathlib import Path

import pytest

from app import config as C
from app import fno_gates as G
from baskfy_core.fno.config import FoSleeve, FoSleeveGroup, group_of
from baskfy_core.options.config import Mode

ROWS = list(itertools.product((False, True), repeat=4))
_FLAG = {FoSleeveGroup.F1: "FNO_F1_EXECUTION_ENABLED", FoSleeveGroup.F2: "FNO_F2_EXECUTION_ENABLED",
         FoSleeveGroup.F3: "FNO_F3_EXECUTION_ENABLED"}
FNO_FLAGS = ("FNO_CARRY_ENABLED", "FNO_F1_EXECUTION_ENABLED", "FNO_F2_EXECUTION_ENABLED",
             "FNO_F3_EXECUTION_ENABLED",
             "FNO_MONITOR_ENABLED")


def _set(monkeypatch, sleeve, dry_off, options, carry, execution) -> None:
    monkeypatch.setattr(C, "DRY_RUN", not dry_off)
    monkeypatch.setattr(C, "OPTIONS_ENABLED", options)
    monkeypatch.setattr(C, "FNO_CARRY_ENABLED", carry)
    for flag in _FLAG.values():
        monkeypatch.setattr(C, flag, False)
    monkeypatch.setattr(C, _FLAG[group_of(sleeve)], execution)


@pytest.mark.parametrize("intraday", (False, True))
@pytest.mark.parametrize("sleeve", list(FoSleeve))
@pytest.mark.parametrize(("dry_off", "options", "carry", "execution"), ROWS)
def test_paper_for_every_row_but_all_four(monkeypatch, sleeve, dry_off, options, carry, execution,
                                          intraday) -> None:
    _set(monkeypatch, sleeve, dry_off, options, carry, execution)
    monkeypatch.setattr(C, "INTRADAY_ENABLED", intraday)  # must make no difference
    all_four = dry_off and options and carry and execution
    assert G.fno_gates(sleeve).mode is (Mode.LIVE if all_four else Mode.PAPER)
    gates = G.product_gates(sleeve)
    assert gates.dry_run is (not all_four)
    assert gates.intraday_enabled is False


def test_every_fo_flag_defaults_false_and_the_shipped_mode_is_paper() -> None:
    for name in FNO_FLAGS:
        assert getattr(C, name) is False, name
    for sleeve in FoSleeve:
        assert G.fno_gates(sleeve).mode is Mode.PAPER
        assert G.product_gates(sleeve).dry_run is True


def test_no_fo_auto_execute_setting_exists() -> None:
    root = Path(__file__).resolve().parents[1] / "app"
    for path in (root / "config.py", root / "fno_gates.py"):
        text = path.read_text(encoding="utf-8").upper()
        assert not re.search(r"BASKFY_FNO_\w*AUTO", text), path.name
        assert not re.search(r"\bFNO_\w*AUTO", text), path.name


def test_the_weekly_desk_gateway_is_untouched_by_the_fo_flags(monkeypatch) -> None:
    from app.core.gateway import _gates_from_config

    for name in FNO_FLAGS:
        monkeypatch.setattr(C, name, True)
    gates = _gates_from_config()
    assert gates.fno_carry_enabled is False
    assert gates.options_enabled is False and gates.dry_run is True


def test_a_paper_fo_future_is_simulated_and_calls_no_broker(tmp_path) -> None:
    """Every money flag false: an F2 NRML leg passes the carry gate under dry_run, simulated."""
    from baskfy_execution import OrderGateway, RiskManager, TenantIds
    from baskfy_execution.guards import FoPlanRef

    class NoBroker:
        def place_order(self, **_: object) -> str:
            raise AssertionError("a PAPER FO leg reached the broker")

    tenant = TenantIds(user_id=1, broker_account_id=1)
    gw = OrderGateway(NoBroker(), RiskManager(), gates=lambda: G.product_gates(FoSleeve.F2),
                      journal_path=str(tmp_path / "j.jsonl"))
    out = asyncio.run(gw.place(symbol="RELIANCE26NOVFUT", qty=250, side="BUY", product="NRML",
                               price=10.0, exchange="NFO", client_id="P:RELIANCE26NOVFUT",
                               tenant=tenant, plan_tenant=tenant, gross_exposure=0.0,
                               fo_plan=FoPlanRef(plan_id="P", sleeve="F2")))
    assert out["status"] == "DRY_RUN", out
