"""docs/options/06 OP2 AC, on the desk: `options_gates()` is PAPER for every combination of the
four switches but all-four-true, per sleeve — the 16-row table x every sleeve, read off
`app.config` the way the desk reads it at the moment of an order.

`product_gates()` is what OP10's gateway will be handed: `dry_run=True` in every PAPER row, so the
gateway's dry-run branch simulates and nothing reaches a broker (non-negotiable 1).
"""
from __future__ import annotations

import asyncio
import itertools

import pytest

from app import config as C
from app import options_gates as G
from baskfy_core.options.config import Mode, Sleeve, SleeveGroup, group_of

ROWS = list(itertools.product((False, True), repeat=4))
_FLAG = {
    SleeveGroup.O1M: "OPTIONS_O1M_EXECUTION_ENABLED",
    SleeveGroup.O1W: "OPTIONS_O1W_EXECUTION_ENABLED",
    SleeveGroup.O2: "OPTIONS_O2_EXECUTION_ENABLED",
    SleeveGroup.O3: "OPTIONS_O3_EXECUTION_ENABLED",
}


def _set(monkeypatch, sleeve: Sleeve, dry_run_off: bool, options: bool, intraday: bool,
         execution: bool) -> None:
    monkeypatch.setattr(C, "DRY_RUN", not dry_run_off)
    monkeypatch.setattr(C, "OPTIONS_ENABLED", options)
    monkeypatch.setattr(C, "INTRADAY_ENABLED", intraday)
    for flag in _FLAG.values():
        monkeypatch.setattr(C, flag, False)
    monkeypatch.setattr(C, _FLAG[group_of(sleeve)], execution)


@pytest.mark.parametrize("sleeve", list(Sleeve))
@pytest.mark.parametrize(("dry_run_off", "options", "intraday", "execution"), ROWS)
def test_paper_for_every_row_but_all_four(monkeypatch, sleeve, dry_run_off, options, intraday,
                                          execution) -> None:
    _set(monkeypatch, sleeve, dry_run_off, options, intraday, execution)
    all_four = dry_run_off and options and intraday and execution
    assert G.options_gates(sleeve).mode is (Mode.LIVE if all_four else Mode.PAPER)
    assert G.product_gates(sleeve).dry_run is (not all_four)


def test_the_shipped_defaults_are_paper_for_every_sleeve() -> None:
    for sleeve in Sleeve:
        assert G.options_gates(sleeve).mode is Mode.PAPER
        assert G.product_gates(sleeve).dry_run is True


def test_the_weekly_desk_gateway_is_untouched_by_the_options_flags(monkeypatch) -> None:
    """The options flags feed only the options book's gates; the weekly desk's default gateway
    still reads DRY_RUN / OPTIONS_ENABLED / INTRADAY_ENABLED and nothing else."""
    from app.core.gateway import _gates_from_config

    for flag in _FLAG.values():
        monkeypatch.setattr(C, flag, True)
    gates = _gates_from_config()
    assert gates.options_enabled is False and gates.intraday_enabled is False
    assert gates.dry_run is True


def test_a_paper_gateway_simulates_an_nfo_mis_leg_and_calls_no_broker(tmp_path) -> None:
    """Every money flag false: the leg passes the product gate under dry_run and is simulated."""
    from baskfy_execution import OrderGateway, RiskManager, TenantIds

    class NoBroker:
        def place_order(self, **_: object) -> str:
            raise AssertionError("a PAPER options leg reached the broker")

    tenant = TenantIds(user_id=1, broker_account_id=1)
    gw = OrderGateway(NoBroker(), RiskManager(), gates=lambda: G.product_gates(Sleeve.O1M),
                      journal_path=str(tmp_path / "j.jsonl"))
    out = asyncio.run(gw.place(symbol="NIFTY2692223700CE", qty=65, side="BUY", product="MIS",
                               order_type="LIMIT", price=10.0, exchange="NFO",
                               client_id="plan:NIFTY2692223700CE", tenant=tenant,
                               plan_tenant=tenant, gross_exposure=650.0))
    assert out["status"] != "BLOCKED", out
