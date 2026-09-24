"""FO7 — F2's evening trail moves its GTT through the gateway (``docs/fno/04`` §10, FO6.4).

``04`` §10: the GTT "is placed within the same session as the fill and modified each evening after
the trail moves", and the trail is **never lowered**. FO6.4 left the trigger-moving modify to FO7:
``modify_gtt_quantity`` with a ``fo_plan`` reference meets the same F2 branch ``place_gtt_stop``
rests the stop under (a stock future on NFO, ``OPTIONS_ENABLED`` and ``BASKFY_FNO_CARRY_ENABLED``
both on, an option GTT refused whatever the switches), keeps the leg ``NRML``, and refuses a
trigger below the one it replaces (``floor_trigger``, required). Every refusal is asserted with a
recording broker that saw nothing.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path

import pytest
from baskfy_execution import OrderGateway, ProductGates, RiskManager, TenantIds
from baskfy_execution.gtt import DRY_RUN_GTT_MODIFY, GTT_MODIFIED
from baskfy_execution.guards import FoPlanRef

TENANT = TenantIds(user_id=1, broker_account_id=1)
F2_REF = FoPlanRef(plan_id="F2-20261027-RELIANCE", sleeve="F2")
F1_REF = FoPlanRef(plan_id="F1N-20261027-NIFTY", sleeve="F1N")
STOCK_FUT = "RELIANCE26NOVFUT"
INDEX_FUT = "NIFTY26NOVFUT"
OPTION = "NIFTY26NOV25500CE"


class SpyKC:
    GTT_TYPE_SINGLE = "single"
    TRANSACTION_TYPE_SELL = "SELL"
    ORDER_TYPE_LIMIT = "LIMIT"
    PRODUCT_CNC = "CNC"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def instruments(self, exchange: str) -> list[dict[str, object]]:
        self.calls.append(("instruments", {"exchange": exchange}))
        return [{"tradingsymbol": STOCK_FUT, "tick_size": 0.10}]

    def modify_gtt(self, **params: object) -> dict[str, int]:
        self.calls.append(("modify_gtt", dict(params)))
        return {"trigger_id": int(str(params["trigger_id"]))}


def _gateway(tmp_path: Path, **gates: bool) -> tuple[OrderGateway, SpyKC, Path]:
    kc = SpyKC()
    journal = tmp_path / "journal.jsonl"
    gw = OrderGateway(
        kc, RiskManager(), gates=lambda: ProductGates(**gates), journal_path=str(journal)
    )
    return gw, kc, journal


def _move(
    gw: OrderGateway,
    *,
    symbol: str = STOCK_FUT,
    trigger: float = 1040.0,
    floor: float | None = 1000.0,
    fo_plan: FoPlanRef | None = F2_REF,
) -> dict[str, object]:
    return asyncio.run(
        gw.modify_gtt_quantity(
            gtt_id=4242,
            symbol=symbol,
            qty=500,
            trigger=trigger,
            last_price=1100.0,
            exchange="NFO",
            tenant=TENANT,
            plan_tenant=TENANT,
            fo_plan=fo_plan,
            floor_trigger=floor,
        )
    )


ON = {"options_enabled": True, "fno_carry_enabled": True}


def test_a_paper_trail_is_journalled_nrml_and_reaches_no_broker(tmp_path: Path) -> None:
    gw, kc, journal = _gateway(tmp_path, dry_run=True, **ON)
    out = _move(gw)
    assert out["status"] == DRY_RUN_GTT_MODIFY
    assert kc.calls == []
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    last = rows[-1]
    assert last["event"] == "gtt_dry_run_modify"
    assert (last["product"], last["fo_plan"], last["floor_trigger"]) == (
        "NRML",
        F2_REF.plan_id,
        1000.0,
    )


def test_with_both_flags_the_trigger_moves_and_the_leg_stays_nrml(tmp_path: Path) -> None:
    gw, kc, _ = _gateway(tmp_path, dry_run=False, **ON)
    out = _move(gw)
    assert out["status"] == GTT_MODIFIED, out
    sent = [params for name, params in kc.calls if name == "modify_gtt"]
    assert len(sent) == 1
    assert sent[0]["trigger_values"] == [1040.0]
    legs = sent[0]["orders"]
    assert isinstance(legs, list) and legs[0]["product"] == "NRML"


@pytest.mark.parametrize("dry_run", (False, True))
def test_a_lower_trigger_is_refused_before_anything_is_sent(tmp_path: Path, dry_run: bool) -> None:
    gw, kc, _ = _gateway(tmp_path, dry_run=dry_run, **ON)
    out = _move(gw, trigger=990.0, floor=1000.0)
    assert out["status"] == "BLOCKED" and "never lowered" in str(out["error"])
    assert kc.calls == []


def test_an_fo_modify_must_name_the_trigger_it_replaces(tmp_path: Path) -> None:
    gw, kc, _ = _gateway(tmp_path, dry_run=False, **ON)
    out = _move(gw, floor=None)
    assert out["status"] == "BLOCKED" and "never lowered" in str(out["error"])
    assert kc.calls == []


@pytest.mark.parametrize(
    ("dry_run", "intraday", "options", "carry"), list(itertools.product((False, True), repeat=4))
)
def test_every_switch_combination(
    tmp_path: Path, dry_run: bool, intraday: bool, options: bool, carry: bool
) -> None:
    gw, kc, _ = _gateway(
        tmp_path,
        dry_run=dry_run,
        intraday_enabled=intraday,
        options_enabled=options,
        fno_carry_enabled=carry,
    )
    out = _move(gw)
    if not (options and carry):
        assert out["status"] == "BLOCKED", out
        assert kc.calls == []
    elif dry_run:
        assert out["status"] == DRY_RUN_GTT_MODIFY
        assert kc.calls == []
    else:
        assert out["status"] == GTT_MODIFIED


@pytest.mark.parametrize(
    ("symbol", "fo_plan"),
    ((OPTION, F2_REF), (OPTION, F1_REF), (INDEX_FUT, F2_REF), (STOCK_FUT, F1_REF)),
)
def test_only_an_f2_stock_future_may_move_its_stop(
    tmp_path: Path, symbol: str, fo_plan: FoPlanRef
) -> None:
    gw, kc, _ = _gateway(tmp_path, dry_run=False, **ON)
    out = _move(gw, symbol=symbol, fo_plan=fo_plan)
    assert out["status"] == "BLOCKED", out
    assert kc.calls == []
