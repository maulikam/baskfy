"""FO6 — the product gate's one new branch and the GTT's one new branch (``docs/fno/02`` §1).

The spec, verbatim from ``docs/fno/02`` §1:

* ``NRML`` on ``NFO`` is admitted only when ``OPTIONS_ENABLED`` **and** ``BASKFY_FNO_CARRY_ENABLED``
  are both true **and** the order carries a ``fo_plan`` reference. **Every other product/venue pair
  is refused exactly as today.**
* A GTT on ``NFO``/``NRML`` for a **stock future** is admitted when the same two flags are on and
  it carries an F2 ``fo_plan`` reference. **A GTT on an option stays refused** (§2.4), whatever the
  switches.

So the table here is every product x every venue x every combination of the three switches and
the reference, asserted against a restatement of the rule; and "exactly as today" is asserted as
string equality with the function called the old way. Every gateway case uses a recording broker
and asserts it saw nothing on a refusal. ``INTRADAY_ENABLED`` is enumerated too, because it must
make no difference to the NRML branch (``02`` Track B: it is not one of the FO switches).
"""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path

import pytest
from baskfy_execution import OrderGateway, ProductGates, RiskManager, TenantIds
from baskfy_execution.gtt import DRY_RUN_GTT, GTT_PLACED
from baskfy_execution.guards import (
    DERIVATIVE_EXCHANGES,
    FoPlanRef,
    product_exchange_refusal,
)

TENANT = TenantIds(user_id=1, broker_account_id=1)
PRODUCTS = ("CNC", "MIS", "NRML", "MTF", "CO", "BO")
EXCHANGES = (*sorted(DERIVATIVE_EXCHANGES), "NSE", "BSE", "XYZ")
FOUR = list(itertools.product((False, True), repeat=4))
F2_REF = FoPlanRef(plan_id="FOPLAN-F2-1", sleeve="F2")
F1_REF = FoPlanRef(plan_id="FOPLAN-F1-1", sleeve="F1N")
STOCK_FUT = "RELIANCE26NOVFUT"
INDEX_FUT = "NIFTY26NOVFUT"
OPTION = "NIFTY26NOV25500CE"


def _spec(  # noqa: PLR0913, PLR0917
    product: str, exchange: str, intraday: bool, options: bool, carry: bool, fo_plan: bool
) -> bool:
    """``02`` §1 restated: the OP2 allow-list, plus the one NRML/NFO branch."""
    old = product_exchange_refusal(
        product, exchange, intraday_enabled=intraday, options_enabled=options
    )
    return old == "" or (exchange == "NFO" and product == "NRML" and options and carry and fo_plan)


# --- the pure allow-list, exhaustively --------------------------------------------------------


@pytest.mark.parametrize("exchange", EXCHANGES)
@pytest.mark.parametrize("product", PRODUCTS)
@pytest.mark.parametrize(("intraday", "options", "carry", "fo_plan"), FOUR)
def test_every_product_venue_and_switch_combination(  # noqa: PLR0913, PLR0917
    product: str, exchange: str, intraday: bool, options: bool, carry: bool, fo_plan: bool
) -> None:
    why = product_exchange_refusal(
        product,
        exchange,
        intraday_enabled=intraday,
        options_enabled=options,
        fno_carry_enabled=carry,
        fo_plan=fo_plan,
    )
    assert (why == "") == _spec(product, exchange, intraday, options, carry, fo_plan), why


@pytest.mark.parametrize("exchange", EXCHANGES)
@pytest.mark.parametrize("product", PRODUCTS)
@pytest.mark.parametrize(("intraday", "options", "carry", "fo_plan"), FOUR)
def test_every_other_pair_is_refused_exactly_as_today(  # noqa: PLR0913, PLR0917
    product: str, exchange: str, intraday: bool, options: bool, carry: bool, fo_plan: bool
) -> None:
    """Outside the NRML/NFO/fo_plan pair the answer is the old function's, word for word."""
    if exchange == "NFO" and product == "NRML" and fo_plan and options:
        pytest.skip("the one pair FO6 adds")
    old = product_exchange_refusal(
        product, exchange, intraday_enabled=intraday, options_enabled=options
    )
    new = product_exchange_refusal(
        product,
        exchange,
        intraday_enabled=intraday,
        options_enabled=options,
        fno_carry_enabled=carry,
        fo_plan=fo_plan,
    )
    assert new == old


@pytest.mark.parametrize("intraday", (False, True))
def test_nrml_on_nfo_with_a_plan_but_no_carry_flag_is_refused_and_says_why(
    intraday: bool,
) -> None:
    why = product_exchange_refusal(
        "NRML", "NFO", intraday_enabled=intraday, options_enabled=True, fo_plan=True
    )
    assert "MIS" in why and "BASKFY_FNO_CARRY_ENABLED" in why


@pytest.mark.parametrize("exchange", sorted(DERIVATIVE_EXCHANGES - {"NFO"}))
def test_the_carry_branch_is_nfo_only(exchange: str) -> None:
    assert product_exchange_refusal(
        "NRML",
        exchange,
        intraday_enabled=True,
        options_enabled=True,
        fno_carry_enabled=True,
        fo_plan=True,
    )


def test_the_carry_flag_does_not_stand_in_for_options_enabled() -> None:
    why = product_exchange_refusal(
        "NRML",
        "NFO",
        intraday_enabled=True,
        options_enabled=False,
        fno_carry_enabled=True,
        fo_plan=True,
    )
    assert "F&O/derivatives disabled (config.OPTIONS_ENABLED)" in why


def test_the_gates_fail_closed_on_the_carry_switch() -> None:
    assert ProductGates().fno_carry_enabled is False


# --- inside the gateway: every product x flag on NFO, with a recording broker -------------------


class SpyKC:
    GTT_TYPE_SINGLE = "single"
    TRANSACTION_TYPE_SELL = "SELL"
    TRANSACTION_TYPE_BUY = "BUY"
    ORDER_TYPE_LIMIT = "LIMIT"
    PRODUCT_CNC = "CNC"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def place_order(self, **params: object) -> str:
        self.calls.append(("place_order", dict(params)))
        return "OID-1"

    def instruments(self, exchange: str) -> list[dict[str, object]]:
        self.calls.append(("instruments", {"exchange": exchange}))
        return [
            {"tradingsymbol": STOCK_FUT, "tick_size": 0.10},
            {"tradingsymbol": "RELIANCE", "tick_size": 0.05},
        ]

    def place_gtt(self, **params: object) -> dict[str, int]:
        self.calls.append(("place_gtt", dict(params)))
        return {"trigger_id": 4242}


def _gateway(tmp_path: Path, **gates: bool) -> tuple[OrderGateway, SpyKC]:
    kc = SpyKC()
    return (
        OrderGateway(
            kc,
            RiskManager(),
            gates=lambda: ProductGates(**gates),
            journal_path=str(tmp_path / "journal.jsonl"),
        ),
        kc,
    )


@pytest.mark.parametrize("product", PRODUCTS)
@pytest.mark.parametrize(("intraday", "options", "carry", "with_plan"), FOUR)
def test_an_nfo_future_through_the_gateway(  # noqa: PLR0913, PLR0917
    tmp_path: Path, product: str, intraday: bool, options: bool, carry: bool, with_plan: bool
) -> None:
    gw, kc = _gateway(
        tmp_path,
        dry_run=False,
        intraday_enabled=intraday,
        options_enabled=options,
        fno_carry_enabled=carry,
    )
    out = asyncio.run(
        gw.place(
            symbol=STOCK_FUT,
            qty=250,
            side="BUY",
            product=product,
            order_type="LIMIT",
            price=10.0,
            exchange="NFO",
            gross_exposure=0.0,
            client_id=f"FOPLAN-F2-1:{STOCK_FUT}",
            tenant=TENANT,
            plan_tenant=TENANT,
            fo_plan=F2_REF if with_plan else None,
        )
    )
    admitted = _spec(product, "NFO", intraday, options, carry, with_plan)
    if admitted:
        assert out["status"] == "PLACED", out
        assert [name for name, _ in kc.calls] == ["place_order"]
        assert kc.calls[0][1]["product"] == product
    else:
        assert out["status"] == "BLOCKED", out
        assert kc.calls == []


def test_the_multi_tenant_clause_refuses_an_fo_order_first(tmp_path: Path) -> None:
    gw, kc = _gateway(tmp_path, dry_run=False, options_enabled=True, fno_carry_enabled=True)
    out = asyncio.run(
        gw.place(
            symbol=STOCK_FUT,
            qty=250,
            side="BUY",
            product="NRML",
            price=10.0,
            exchange="NFO",
            gross_exposure=0.0,
            tenant=TENANT,
            plan_tenant=TenantIds(user_id=2, broker_account_id=1),
            fo_plan=F2_REF,
        )
    )
    assert out["status"] == "BLOCKED" and "tenant mismatch" in str(out["error"])
    assert kc.calls == []


# --- GTT: F2's stock future is the one derivative GTT; an option GTT never is ------------------


def _arm(gw: OrderGateway, symbol: str, fo_plan: FoPlanRef | None) -> dict[str, object]:
    return asyncio.run(
        gw.place_gtt_stop(
            symbol=symbol,
            qty=250,
            trigger=89.0,
            last_price=100.0,
            exchange="NFO",
            client_id=f"PLAN:{symbol}",
            tenant=TENANT,
            plan_tenant=TENANT,
            fo_plan=fo_plan,
        )
    )


@pytest.mark.parametrize("fo_plan", (None, F1_REF, F2_REF), ids=("no-plan", "F1", "F2"))
@pytest.mark.parametrize(("dry_run", "intraday", "options", "carry"), FOUR)
def test_an_option_gtt_is_refused_whatever_the_switches_and_the_plan(  # noqa: PLR0913, PLR0917
    tmp_path: Path,
    fo_plan: FoPlanRef | None,
    dry_run: bool,
    intraday: bool,
    options: bool,
    carry: bool,
) -> None:
    gw, kc = _gateway(
        tmp_path,
        dry_run=dry_run,
        intraday_enabled=intraday,
        options_enabled=options,
        fno_carry_enabled=carry,
    )
    out = _arm(gw, OPTION, fo_plan)
    assert out["status"] == "BLOCKED", out
    assert kc.calls == []


@pytest.mark.parametrize(("dry_run", "intraday", "options", "carry"), FOUR)
def test_an_f2_stock_future_gtt_needs_both_flags(
    tmp_path: Path, dry_run: bool, intraday: bool, options: bool, carry: bool
) -> None:
    gw, kc = _gateway(
        tmp_path,
        dry_run=dry_run,
        intraday_enabled=intraday,
        options_enabled=options,
        fno_carry_enabled=carry,
    )
    out = _arm(gw, STOCK_FUT, F2_REF)
    if not (options and carry):
        assert out["status"] == "BLOCKED", out
        assert kc.calls == []
        return
    if dry_run:
        assert out["status"] == DRY_RUN_GTT
        assert kc.calls == []
        return
    assert out["status"] == GTT_PLACED, out
    placed = [params for name, params in kc.calls if name == "place_gtt"]
    assert len(placed) == 1
    legs = placed[0]["orders"]
    assert isinstance(legs, list)
    assert legs[0]["product"] == "NRML"
    assert legs[0]["transaction_type"] == "SELL"


@pytest.mark.parametrize(
    ("symbol", "fo_plan"),
    (
        (INDEX_FUT, F2_REF),  # a stock future only
        (STOCK_FUT, F1_REF),  # an F2 reference only
        (STOCK_FUT, None),  # the old refusal, as today
        (STOCK_FUT, FoPlanRef(plan_id="", sleeve="F2")),
        (STOCK_FUT, FoPlanRef(plan_id="P", sleeve="O1M")),
    ),
)
def test_every_other_nfo_gtt_is_refused_with_both_flags_on(
    tmp_path: Path, symbol: str, fo_plan: FoPlanRef | None
) -> None:
    gw, kc = _gateway(
        tmp_path,
        dry_run=False,
        intraday_enabled=True,
        options_enabled=True,
        fno_carry_enabled=True,
    )
    out = _arm(gw, symbol, fo_plan)
    assert out["status"] == "BLOCKED", out
    assert kc.calls == []


def test_an_f2_reference_does_not_open_a_gtt_on_another_venue(tmp_path: Path) -> None:
    gw, kc = _gateway(tmp_path, dry_run=False, options_enabled=True, fno_carry_enabled=True)
    out = asyncio.run(
        gw.place_gtt_stop(
            symbol=STOCK_FUT,
            qty=250,
            trigger=89.0,
            last_price=100.0,
            exchange="BFO",
            tenant=TENANT,
            plan_tenant=TENANT,
            fo_plan=F2_REF,
        )
    )
    assert out["status"] == "BLOCKED"
    assert kc.calls == []


def test_a_cash_gtt_is_byte_for_byte_what_it_was(tmp_path: Path) -> None:
    """The carry switch on changes nothing for the weekly book's stop: CNC on NSE."""
    gw, kc = _gateway(tmp_path, dry_run=False, options_enabled=True, fno_carry_enabled=True)
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
    assert out["status"] == GTT_PLACED
    placed = [params for name, params in kc.calls if name == "place_gtt"]
    legs = placed[0]["orders"]
    assert isinstance(legs, list)
    assert legs[0]["product"] == "CNC"
