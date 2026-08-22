"""The desk's seven non-negotiables, one named test each (MERGE-PROMPTS.md M16 step 1).

`CLAUDE.md` carries these under the heading "do not 'improve' these away", and every one is a
scar: each was written after something went wrong, and the comment above it in the source usually
says what. They now live across three trees — the desk, `baskfy_core` and `baskfy_execution` — so
this file is the single place a reviewer can check that all seven still hold.

Structural where the property is structural (the order path's four layers appear in order; nothing
outside the gateway calls `place_order`) and behavioural where it can be exercised (an SGB is
refused, a gate is shut). Where a test here duplicates one elsewhere, that is deliberate: this file
is the index, and an index that points at nothing is how a rule quietly stops being enforced.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest

from ._source import src_of


# =====================================================================================
# 1. Never auto-execute
# =====================================================================================
def test_non_negotiable_1_execution_requires_explicit_confirmation_and_a_fresh_plan() -> None:
    """Orders fire only from POST /execute with confirm=true and a plan_id from this session,
    and the plan expires in 30 minutes so nobody fires on stale prices."""
    from app import main

    src = src_of(main)
    assert 'if confirm != "true":' in src, "the confirm gate is gone"
    assert 'raise HTTPException(400, "Execution requires explicit confirmation.")' in src
    assert 'plan = PLANS.get(plan_id)' in src, "execution no longer requires a known plan_id"
    assert src.count('time.time() - plan["created_at"] > 1800') >= 1, (
        "the 30-minute plan expiry is gone; a stale plan prices orders off stale quotes"
    )


def test_non_negotiable_1b_dry_run_simulates_end_to_end() -> None:
    """DRY_RUN must place nothing and still return a well-formed result."""
    from baskfy_execution import OrderGateway, ProductGates, RiskManager

    class ExplodingKC:
        def place_order(self, **_: object) -> str:
            raise AssertionError("DRY_RUN placed a real order")

    gw = OrderGateway(ExplodingKC(), RiskManager(),
                      gates=lambda: ProductGates(dry_run=True),
                      journal_path="data/outputs/_test_journal.jsonl")
    out = asyncio.run(gw.place(symbol="RELIANCE", qty=1, side="BUY", product="CNC",
                               order_type="LIMIT", price=100.0, exchange="NSE"))
    assert out["status"] == "DRY_RUN"
    assert out["order_id"].startswith("DRY-")


# =====================================================================================
# 2. Holdings quantity includes pledged and T1
# =====================================================================================
def test_non_negotiable_2_holdings_count_pledged_and_t1() -> None:
    """`quantity + t1_quantity + collateral_quantity`. Missing this once caused a 103-share
    position error: pledged shares are held, and a plan that cannot see them sells what is
    not there."""
    from app import kite_client

    src = src_of(kite_client)
    assert 'h["quantity"] + h.get("t1_quantity", 0) + h.get("collateral_quantity", 0)' in src


# =====================================================================================
# 3. Pledged shares sell directly; the plan only flags them
# =====================================================================================
def test_non_negotiable_3_pledged_shares_are_information_not_a_gate() -> None:
    """Zerodha's instant-sale means pledged stock sells without an unpledge step. The plan
    reports the collateral impact; it must not refuse or block on it."""
    from app import kite_client

    src = src_of(kite_client)
    assert "pledged_qty" in src, "the pledged quantity is no longer surfaced at all"


# =====================================================================================
# 4. Every buy gets a GTT stop, vol-scaled
# =====================================================================================
def test_non_negotiable_4_every_buy_is_stopped_and_the_stop_is_vol_scaled() -> None:
    """`stop_from_vol` clamps to the configured floor and ceiling — 8-12% by default."""
    from app import config as C
    from baskfy_core.score import stop_from_vol

    quiet = stop_from_vol(100.0, 0.05, C)       # very low vol -> the floor
    wild = stop_from_vol(100.0, 5.0, C)         # very high vol -> the ceiling
    assert quiet == pytest.approx(100.0 * (1 - C.STOP_MIN), abs=0.15)
    assert wild == pytest.approx(100.0 * (1 - C.STOP_MAX), abs=0.15)
    assert C.STOP_MIN < C.STOP_MAX, "the stop band is inverted"


# =====================================================================================
# 5. Product gates: CNC only unless explicitly enabled, enforced INSIDE the gateway
# =====================================================================================
@pytest.mark.parametrize(
    ("product", "exchange", "gates", "why"),
    [
        ("MIS", "NSE", {"intraday_enabled": False}, "MIS/intraday disabled"),
        ("NRML", "NFO", {"options_enabled": False}, "F&O disabled"),
        ("NRML", "BFO", {"options_enabled": False}, "F&O disabled"),
    ],
)
def test_non_negotiable_5_product_gates_block_inside_the_gateway(
    product: str, exchange: str, gates: dict[str, bool], why: str
) -> None:
    from baskfy_execution import OrderGateway, ProductGates, RiskManager

    class ExplodingKC:
        def place_order(self, **_: object) -> str:
            raise AssertionError("a gated product reached the broker")

    gw = OrderGateway(ExplodingKC(), RiskManager(),
                      gates=lambda: ProductGates(dry_run=False, **gates),
                      journal_path="data/outputs/_test_journal.jsonl")
    out = asyncio.run(gw.place(symbol="RELIANCE", qty=1, side="BUY", product=product,
                               order_type="LIMIT", price=100.0, exchange=exchange))
    assert out["status"] == "BLOCKED"
    assert why in out["error"]


def test_non_negotiable_5b_the_gates_fail_closed_when_nobody_supplies_them() -> None:
    """A default that permits is the wrong default for the module that talks to a broker."""
    from baskfy_execution import ProductGates

    default = ProductGates()
    assert default.dry_run is True
    assert default.intraday_enabled is False
    assert default.options_enabled is False


# =====================================================================================
# 6. All order flow goes through the gateway
# =====================================================================================
def test_non_negotiable_6_the_four_layers_are_in_order() -> None:
    """guards -> risk -> idempotency -> rate limits. Reordering them is how an untouchable
    instrument reaches a network call."""
    from baskfy_execution import gateway

    src = src_of(gateway)
    marks = ["layer 1: untouchables", "layer 2: risk",
             "layer 3: idempotency", "layer 4: rate limits"]
    positions = [src.index(m) for m in marks]
    assert positions == sorted(positions)


def test_non_negotiable_6b_nothing_outside_the_gateway_calls_place_order() -> None:
    """The desk's own modules must reach the broker through the gateway, never around it."""
    app_dir = pathlib.Path("app")
    offenders = []
    for path in sorted(app_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "place_order"):
                offenders.append(str(path))
    # kite_client wraps the broker SDK and is what the gateway itself calls.
    offenders = [o for o in offenders if not o.endswith("kite_client.py")]
    assert offenders == [], f"{offenders} call place_order outside the gateway"


def test_non_negotiable_6c_the_client_id_makes_a_replayed_plan_idempotent() -> None:
    """`client_id = plan_id:symbol`, so re-posting a plan cannot double-send."""
    from app import main

    assert 'client_id=f"{plan_id}:{o[\'symbol\']}"' in src_of(main)


# =====================================================================================
# 7. Untouchable instruments, at the lowest layer
# =====================================================================================
@pytest.mark.parametrize("symbol", ["SGBDE31III", "SGBDE31III-GB", "SGBJUN29"])
def test_non_negotiable_7_an_untouchable_is_refused_before_any_network_call(symbol: str) -> None:
    """Prefix- and series-aware, not an exact-match set. The set held "SGBDE31III" while the
    holding was "SGBDE31III-GB", and the planner proposed EXIT -392 on a Rs 60 lakh position."""
    from baskfy_execution import UntouchableInstrumentError, assert_tradeable

    with pytest.raises(UntouchableInstrumentError):
        assert_tradeable(symbol)


def test_non_negotiable_7b_the_guard_runs_before_the_broker_is_touched() -> None:
    from baskfy_execution import OrderGateway, ProductGates, RiskManager, UntouchableInstrumentError

    class ExplodingKC:
        def place_order(self, **_: object) -> str:
            raise AssertionError("the broker was reached for an untouchable instrument")

    gw = OrderGateway(ExplodingKC(), RiskManager(),
                      gates=lambda: ProductGates(dry_run=False),
                      journal_path="data/outputs/_test_journal.jsonl")
    with pytest.raises(UntouchableInstrumentError):
        asyncio.run(gw.place(symbol="SGBDE31III-GB", qty=1, side="SELL", product="CNC",
                             order_type="LIMIT", price=7000.0, exchange="NSE"))
