"""The desk's seven non-negotiables, as `baskfy_execution` holds them.

`packages/execution/src/baskfy_execution/__init__.py` has claimed since M16 that

    "Each of the desk's seven non-negotiables has a named test in
     `packages/execution/tests/test_non_negotiables.py` that fails if it is 'improved' away."

That file did not exist. This is it. The named tests it promised lived only in
`kite-momentum-rebalancer/tests/test_seven_non_negotiables.py`, which runs inside the desk's own
tree and against the desk's own environment — so the package that is "the only path to an order"
shipped with no index of the rules it exists to enforce, and the execution suite could go green
while four of them were unasserted.

WHAT IS HERE AND WHAT IS NOT. Three of the seven are properties of the desk's web application,
not of this package: #1's `confirm=true` gate and 30-minute plan expiry, #2's
`quantity + t1_quantity + collateral_quantity`, and #3's pledged-shares-are-information rule all
live in `app/main.py` and `app/kite_client.py`. Their named tests stay in the desk's file
(`test_seven_non_negotiables.py:27,63,76,173`) and are cited here rather than duplicated: a copy
that cannot import what it asserts would have to scan the desk's source across a tree boundary,
which is a test of a file path more than of a rule. The four that ARE this package's — and the
parts of #1 and #6 that are — are asserted below.

Ported from `kite-momentum-rebalancer/tests/test_seven_non_negotiables.py`, with #4 and #6
extended by leaf 1.2.1: the GTT path is now the gateway's, so non-negotiable 6's documented
exception ("the gateway has no GTT method yet") is closed and testable here.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest
from baskfy_execution import (
    OrderGateway,
    ProductGates,
    RiskManager,
    TenantIds,
    UntouchableInstrumentError,
)
from baskfy_execution import (
    gateway as gateway_module,
)
from baskfy_execution.gtt import DRY_RUN_GTT, StopBand

SRC = pathlib.Path(gateway_module.__file__).resolve().parent

TENANT = TenantIds(user_id=1, broker_account_id=1)


class ExplodingKC:
    """Anything that reaches this object is a rule that did not hold."""

    GTT_TYPE_SINGLE = "single"
    TRANSACTION_TYPE_SELL = "SELL"
    ORDER_TYPE_LIMIT = "LIMIT"
    PRODUCT_CNC = "CNC"

    def place_order(self, **_: object) -> str:
        raise AssertionError("a refused order reached the broker")

    def place_gtt(self, **_: object) -> dict[str, int]:
        raise AssertionError("a refused GTT reached the broker")

    def delete_gtt(self, **_: object) -> dict[str, int]:
        raise AssertionError("a refused GTT cancellation reached the broker")

    def instruments(self, exchange: str) -> list[dict[str, object]]:
        raise AssertionError("the instrument dump was fetched for a refused GTT")


def make_gateway(tmp_path: pathlib.Path, **gate_kwargs: bool) -> OrderGateway:
    return OrderGateway(
        ExplodingKC(),
        RiskManager(),
        gates=lambda: ProductGates(**gate_kwargs),
        journal_path=str(tmp_path / "journal.jsonl"),
    )


# =====================================================================================
# 1. Never auto-execute
#
# The `confirm=true` gate, the plan_id and the 30-minute expiry are the desk's
# (`test_seven_non_negotiables.py:27`). What belongs here is the half this package owns:
# DRY_RUN must simulate end to end and place nothing.
# =====================================================================================
def test_non_negotiable_1_dry_run_simulates_an_order_end_to_end(
    tmp_path: pathlib.Path,
) -> None:
    gw = make_gateway(tmp_path, dry_run=True)
    out = asyncio.run(
        gw.place(
            symbol="RELIANCE",
            qty=1,
            side="BUY",
            product="CNC",
            order_type="LIMIT",
            price=100.0,
            exchange="NSE",
            tenant=TENANT,
            plan_tenant=TENANT,
        )
    )
    assert out["status"] == "DRY_RUN"
    assert str(out["order_id"]).startswith("DRY-")


def test_non_negotiable_1b_dry_run_simulates_a_gtt_end_to_end(
    tmp_path: pathlib.Path,
) -> None:
    """Added by leaf 1.2.1. A stop is as capable of touching the account as an order, so
    DRY_RUN has to cover it or "simulate end to end" is only true of half the system."""
    gw = make_gateway(tmp_path, dry_run=True)
    out = asyncio.run(
        gw.place_gtt_stop(
            symbol="RELIANCE",
            qty=10,
            trigger=89.0,
            last_price=100.0,
            tenant=TENANT,
            plan_tenant=TENANT,
        )
    )
    assert out["status"] == DRY_RUN_GTT
    assert out["trigger"] == 89.0


def test_non_negotiable_1c_dry_run_simulates_a_cancellation_too(
    tmp_path: pathlib.Path,
) -> None:
    gw = make_gateway(tmp_path, dry_run=True)
    out = asyncio.run(gw.delete_gtt(gtt_id=1, symbol="RELIANCE", tenant=TENANT, plan_tenant=TENANT))
    assert out["status"] == "DRY_RUN_GTT_DELETE"


# =====================================================================================
# 2. Holdings quantity = quantity + t1_quantity + collateral_quantity
# 3. Pledged shares sell directly; the plan only flags them
#
# Both are `app/kite_client.py`'s, asserted at `test_seven_non_negotiables.py:63,76`.
# `packages/execution` never reads a holding.
# =====================================================================================


# =====================================================================================
# 4. Every buy gets a GTT stop, vol-scaled 8-12%
# =====================================================================================
def test_non_negotiable_4_the_stop_band_is_eight_to_twelve_percent() -> None:
    """`stop_from_vol`'s own clamp, and the band the gateway journals against, are the same
    two numbers. The value-level proof against the desk's outputs is in
    `test_stop_from_vol.py`."""
    band = StopBand()
    assert (band.min_pct, band.max_pct) == (0.08, 0.12)
    assert band.min_pct < band.max_pct, "the stop band is inverted"


def test_non_negotiable_4b_a_stop_can_be_armed_through_the_gateway(
    tmp_path: pathlib.Path,
) -> None:
    """Leaf 1.2.1. Before it, `OrderGateway` had no GTT method at all and rule 4 was
    satisfiable only by calling the broker wrapper directly."""
    assert hasattr(OrderGateway, "place_gtt_stop")
    assert hasattr(OrderGateway, "delete_gtt")
    gw = make_gateway(tmp_path, dry_run=True)
    out = asyncio.run(
        gw.place_gtt_stop(
            symbol="RELIANCE",
            qty=10,
            trigger=89.0,
            last_price=100.0,
            tenant=TENANT,
            plan_tenant=TENANT,
        )
    )
    assert out["status"] in ("GTT_PLACED", DRY_RUN_GTT)


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
    product: str, exchange: str, gates: dict[str, bool], why: str, tmp_path: pathlib.Path
) -> None:
    gw = make_gateway(tmp_path, dry_run=False, **gates)
    out = asyncio.run(
        gw.place(
            symbol="RELIANCE",
            qty=1,
            side="BUY",
            product=product,
            order_type="LIMIT",
            price=100.0,
            exchange=exchange,
            tenant=TENANT,
            plan_tenant=TENANT,
        )
    )
    assert out["status"] == "BLOCKED"
    assert why in str(out["error"])


def test_non_negotiable_5b_a_gtt_is_gated_on_the_same_switch(
    tmp_path: pathlib.Path,
) -> None:
    gw = make_gateway(tmp_path, dry_run=False, options_enabled=False)
    out = asyncio.run(
        gw.place_gtt_stop(
            symbol="NIFTY25SEPFUT",
            qty=50,
            trigger=89.0,
            last_price=100.0,
            exchange="NFO",
            tenant=TENANT,
            plan_tenant=TENANT,
        )
    )
    assert out["status"] == "BLOCKED"
    assert "F&O disabled" in str(out["error"])
    # An OPTION on the same exchange is refused one layer earlier, by the overnight guard —
    # the same precedence `place()` uses, so enabling options cannot open a GTT door.
    out = asyncio.run(
        gw.place_gtt_stop(
            symbol="NIFTY25SEP24000CE",
            qty=50,
            trigger=89.0,
            last_price=100.0,
            exchange="NFO",
            tenant=TENANT,
            plan_tenant=TENANT,
        )
    )
    assert out["status"] == "BLOCKED"
    assert "past the close" in str(out["error"])


def test_non_negotiable_5c_the_gates_fail_closed_when_nobody_supplies_them() -> None:
    """A default that permits is the wrong default for the module that talks to a broker."""
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
    src = (SRC / "gateway.py").read_text(encoding="utf-8")
    marks = [
        "layer 1: untouchables",
        "layer 2: risk",
        "layer 3: idempotency",
        "layer 4: rate limits",
    ]
    positions = [src.index(m) for m in marks]
    assert positions == sorted(positions)


def test_non_negotiable_6b_the_gtt_path_has_the_same_four_layers() -> None:
    """CLAUDE.md's caveat on rule 6 — "the gateway has no GTT method yet" — is what leaf 1.2.1
    closed. The GTT path carries the layers under their own names so this can be checked."""
    src = (SRC / "gateway.py").read_text(encoding="utf-8")
    marks = [
        "GTT layer 1: untouchables",
        "GTT layer 2: risk",
        "GTT layer 3: idempotency",
        "GTT layer 4: rate limits",
    ]
    positions = [src.index(m) for m in marks]
    assert positions == sorted(positions)


def test_non_negotiable_6c_nothing_in_this_package_reaches_a_gtt_outside_the_gateway() -> None:
    """The structural half of "the gateway is the only path".

    A guard on a broker wrapper is one import away from being bypassed, which is exactly what
    `kite_client.place_gtt_stop` was. Within `baskfy_execution` the broker's GTT verbs may be
    called from `gateway.py` and nowhere else.
    """
    verbs = {"place_gtt", "delete_gtt", "modify_gtt", "place_order"}
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts or path.name == "gateway.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in verbs
            ):
                offenders.append(f"{path.name}:{node.lineno} {node.func.attr}")
    assert offenders == [], f"{offenders} reach the broker outside the gateway"


def test_non_negotiable_6d_a_replayed_client_id_cannot_double_send(
    tmp_path: pathlib.Path,
) -> None:
    """`client_id = plan_id:symbol`, so re-posting a plan cannot double-send. The desk builds
    that id (`test_seven_non_negotiables.py:173`); the gateway is what honours it."""
    gw = make_gateway(tmp_path, dry_run=True)

    def once() -> dict[str, object]:
        return asyncio.run(
            gw.place(
                symbol="RELIANCE",
                qty=1,
                side="BUY",
                product="CNC",
                order_type="LIMIT",
                price=100.0,
                exchange="NSE",
                client_id="PLAN1:RELIANCE",
                tenant=TENANT,
                plan_tenant=TENANT,
            )
        )

    assert once()["status"] == "DRY_RUN"
    assert once()["status"] == "DUPLICATE"


# =====================================================================================
# 7. Untouchable instruments, at the lowest layer
# =====================================================================================
@pytest.mark.parametrize("symbol", ["SGBDE31III", "SGBDE31III-GB", "SGBJUN29"])
def test_non_negotiable_7_an_untouchable_is_refused_before_any_network_call(
    symbol: str, tmp_path: pathlib.Path
) -> None:
    """Prefix- and series-aware, not an exact-match set. The set held "SGBDE31III" while the
    holding was "SGBDE31III-GB", and the planner proposed EXIT -392 on a Rs 60 lakh position."""
    gw = make_gateway(tmp_path, dry_run=False)
    with pytest.raises(UntouchableInstrumentError):
        asyncio.run(
            gw.place(
                symbol=symbol,
                qty=1,
                side="SELL",
                product="CNC",
                order_type="LIMIT",
                price=7000.0,
                exchange="NSE",
                tenant=TENANT,
                plan_tenant=TENANT,
            )
        )


@pytest.mark.parametrize("symbol", ["SGBDE31III", "SGBDE31III-GB", "SGBJUN29"])
def test_non_negotiable_7b_an_untouchable_is_refused_a_gtt_in_both_directions(
    symbol: str, tmp_path: pathlib.Path
) -> None:
    """Leaf 1.2.1. Arming and cancelling are both refused, and both before the broker is
    reached — `ExplodingKC` proves the second half."""
    gw = make_gateway(tmp_path, dry_run=False)
    with pytest.raises(UntouchableInstrumentError):
        asyncio.run(
            gw.place_gtt_stop(
                symbol=symbol,
                qty=1,
                trigger=6300.0,
                last_price=7000.0,
                tenant=TENANT,
                plan_tenant=TENANT,
            )
        )
    with pytest.raises(UntouchableInstrumentError):
        asyncio.run(gw.delete_gtt(gtt_id=1, symbol=symbol, tenant=TENANT, plan_tenant=TENANT))


# =====================================================================================
# The risk layer values every order, including one that carries no price of its own
#
# Not one of the seven by name, but the thing that makes several of them mean anything:
# `RiskManager.pre_order` is handed `abs(qty) * price`, and every notional cap it holds is
# a comparison against that number. It used to be computed as `float(price or 0)`, which
# was correct while every order this codebase sent carried a price. The swing book's entry
# became a MARKET order on 4 Sep 2026 (Maulik) and a MARKET order has no price — so that
# expression would have valued every entry at ZERO and passed it through every cap, with
# the caps still present, still tested, and silently applying to nothing.
# =====================================================================================
def test_a_market_order_is_valued_for_risk_by_its_reference_price(
    tmp_path: pathlib.Path,
) -> None:
    """The reference price the caller read is what the risk layer sizes against."""
    seen: list[tuple[str, float, float]] = []

    class RecordingRisk(RiskManager):
        def pre_order(
            self, symbol: str, value: float, gross_exposure: float = 0.0
        ) -> tuple[bool, str]:
            seen.append((symbol, value, gross_exposure))
            return True, ""

    gw = OrderGateway(
        ExplodingKC(),
        RecordingRisk(),
        gates=lambda: ProductGates(dry_run=True),
        journal_path=str(tmp_path / "journal.jsonl"),
    )
    asyncio.run(
        gw.place(
            symbol="RELIANCE",
            qty=100,
            side="BUY",
            order_type="MARKET",
            price=None,
            reference_price=250.0,
            market_protection=0.5,
            tenant=TENANT,
            plan_tenant=TENANT,
        )
    )
    assert seen == [("RELIANCE", 25_000.0, 0.0)], "a MARKET order was valued at zero"


def test_an_order_with_no_price_and_no_reference_is_blocked_not_valued_at_zero(
    tmp_path: pathlib.Path,
) -> None:
    """The failure mode this guards: an unpriced order sailing past every notional cap.

    Refusing is recoverable — the caller reads a live price and posts again. An order
    placed against a risk check that could not value it is not.
    """
    gw = make_gateway(tmp_path, dry_run=True)
    out = asyncio.run(
        gw.place(
            symbol="RELIANCE",
            qty=100,
            side="BUY",
            order_type="MARKET",
            price=None,
            reference_price=None,
            tenant=TENANT,
            plan_tenant=TENANT,
        )
    )
    assert out["status"] == "BLOCKED"
    assert "risk check cannot value it" in out["error"]
    journal = (tmp_path / "journal.jsonl").read_text()
    assert "unpriced_order_block" in journal, "the refusal is on the record"


def test_market_protection_reaches_the_broker_only_on_a_market_order(
    tmp_path: pathlib.Path,
) -> None:
    """Kite ignores `market_protection` on a LIMIT; sending it there would put a number in
    the journal that had no effect on the order — a lie in the one record we keep."""
    sent: list[dict[str, object]] = []

    class RecordingKC(ExplodingKC):
        def place_order(self, **kw: object) -> str:
            sent.append(kw)
            return "ORDER-1"

    def gateway() -> OrderGateway:
        return OrderGateway(
            RecordingKC(),
            RiskManager(),
            gates=lambda: ProductGates(dry_run=False),
            journal_path=str(tmp_path / "journal.jsonl"),
        )

    asyncio.run(
        gateway().place(
            symbol="RELIANCE",
            qty=1,
            side="BUY",
            order_type="MARKET",
            price=None,
            reference_price=100.0,
            market_protection=0.44,
            tenant=TENANT,
            plan_tenant=TENANT,
            client_id="a",
        )
    )
    asyncio.run(
        gateway().place(
            symbol="RELIANCE",
            qty=1,
            side="BUY",
            order_type="LIMIT",
            price=100.0,
            market_protection=0.44,
            tenant=TENANT,
            plan_tenant=TENANT,
            client_id="b",
        )
    )
    assert sent[0]["order_type"] == "MARKET"
    assert sent[0]["market_protection"] == 0.44
    assert "price" not in sent[0], "a MARKET order carries no price"
    assert sent[1]["order_type"] == "LIMIT"
    assert "market_protection" not in sent[1]
