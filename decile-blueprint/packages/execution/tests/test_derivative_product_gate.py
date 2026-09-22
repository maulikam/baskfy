"""Non-negotiable 5 on derivative venues — OP2, closing the gap DECISIONS-OP OP0.6 found.

The non-negotiable, verbatim from the root ``CLAUDE.md``:

    Product gates: CNC-only; MIS needs ``INTRADAY_ENABLED``, NFO/BFO needs ``OPTIONS_ENABLED``;
    both default off, enforced inside the gateway.

Until OP2 the allow-list (AF 0.7) returned "" for **any** product on a ``DERIVATIVE_EXCHANGES``
venue once ``options_enabled`` was true, so ``OPTIONS_ENABLED`` alone admitted an NFO **MIS**
order without ``INTRADAY_ENABLED`` and an **NRML future** (``assert_not_overnight_option`` only
refuses *options* under a carry product). Both contradict the sentence above: MIS needs the
intraday switch on every venue, and nothing in this system is allowed to carry a derivative
overnight (futures are ``docs/options/02`` Track C §6; options are MIS-only since 16 Aug 2026).

These tests assert the non-negotiable, not the code: a derivative venue admits **only MIS**, and
only with **both** switches on. Every combination of the two switches is enumerated so a future
"simplification" that drops one conjunct fails here by name.
"""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path

import pytest
from baskfy_execution import OrderGateway, ProductGates, RiskConfig, RiskManager, TenantIds
from baskfy_execution.guards import (
    CARRY_PRODUCTS,
    DERIVATIVE_EXCHANGES,
    product_exchange_refusal,
)

TENANT = TenantIds(user_id=1, broker_account_id=1)

#: Every product string the desk's code has ever sent, plus the two Kite knows that it never has.
PRODUCTS = ("CNC", "MIS", "NRML", "MTF", "CO", "BO")
SWITCHES = list(itertools.product((False, True), repeat=2))


class _NoBroker:
    """A Kite client that records instead of sending. A gated order must never reach it."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def place_order(self, **_: object) -> str:
        self.calls.append("place_order")
        return "OID-1"

    def ltp(self, *_: object) -> dict[str, object]:
        return {}


def _gateway(tmp_path: Path, **gates: bool) -> tuple[OrderGateway, _NoBroker]:
    kc = _NoBroker()
    return (
        OrderGateway(
            kc,
            RiskManager(RiskConfig()),
            gates=lambda: ProductGates(**gates),
            journal_path=str(tmp_path / "journal.jsonl"),
        ),
        kc,
    )


def _place(gw: OrderGateway, *, symbol: str, product: str, exchange: str) -> dict[str, object]:
    return asyncio.run(
        gw.place(
            symbol=symbol,
            qty=65,
            side="BUY",
            product=product,
            order_type="LIMIT",
            price=100.0,
            exchange=exchange,
            gross_exposure=6500.0,
            client_id=f"test:{symbol}:{product}:{exchange}",
            tenant=TENANT,
            plan_tenant=TENANT,
        )
    )


# --- the pure allow-list, exhaustively ----------------------------------------------------------


@pytest.mark.parametrize("exchange", sorted(DERIVATIVE_EXCHANGES))
@pytest.mark.parametrize("product", PRODUCTS)
@pytest.mark.parametrize(("intraday", "options"), SWITCHES)
def test_a_derivative_venue_admits_mis_only_and_only_with_both_switches(
    exchange: str, product: str, intraday: bool, options: bool
) -> None:
    why = product_exchange_refusal(
        product, exchange, intraday_enabled=intraday, options_enabled=options
    )
    admitted = why == ""
    assert admitted == (product == "MIS" and intraday and options), (
        f"{product} on {exchange} with intraday={intraday} options={options}: "
        f"{'admitted' if admitted else why!r}"
    )


@pytest.mark.parametrize("exchange", sorted(DERIVATIVE_EXCHANGES))
def test_options_enabled_alone_no_longer_admits_mis(exchange: str) -> None:
    """OP0.6's exact case: the flag that names F&O does not also switch on intraday."""
    why = product_exchange_refusal("MIS", exchange, intraday_enabled=False, options_enabled=True)
    assert "INTRADAY_ENABLED" in why


@pytest.mark.parametrize("exchange", sorted(DERIVATIVE_EXCHANGES))
@pytest.mark.parametrize("product", sorted(CARRY_PRODUCTS))
def test_a_carry_product_on_a_derivative_venue_is_refused_even_with_both_switches(
    exchange: str, product: str
) -> None:
    """NRML futures were the second half of OP0.6. The refusal names the rule, not a flag —
    no switch in this system turns a carry product on for a derivative venue."""
    why = product_exchange_refusal(product, exchange, intraday_enabled=True, options_enabled=True)
    assert why
    assert "MIS" in why


@pytest.mark.parametrize("exchange", sorted(DERIVATIVE_EXCHANGES))
@pytest.mark.parametrize("product", PRODUCTS)
def test_without_options_enabled_the_refusal_names_that_switch_first(
    exchange: str, product: str
) -> None:
    """The existing wording is kept for this case — the desk's own tests and the swing Track C
    tests match on it, and 'F&O disabled' is the true first reason."""
    why = product_exchange_refusal(product, exchange, intraday_enabled=True, options_enabled=False)
    assert "F&O/derivatives disabled (config.OPTIONS_ENABLED)" in why


@pytest.mark.parametrize(("intraday", "options"), SWITCHES)
def test_the_cash_venues_are_unchanged(intraday: bool, options: bool) -> None:
    """OP2 touches the derivative branch only. CNC on NSE/BSE is always admitted; MIS on cash
    needs the intraday switch exactly as before; nothing else is admitted on cash."""
    for exchange in ("NSE", "BSE"):
        assert (
            product_exchange_refusal(
                "CNC", exchange, intraday_enabled=intraday, options_enabled=options
            )
            == ""
        )
        mis = product_exchange_refusal(
            "MIS", exchange, intraday_enabled=intraday, options_enabled=options
        )
        assert (mis == "") == intraday
        assert product_exchange_refusal(
            "NRML", exchange, intraday_enabled=intraday, options_enabled=options
        )


# --- inside the gateway, before any network call -------------------------------------------------


def test_an_nfo_mis_option_is_blocked_in_the_gateway_with_options_but_not_intraday(
    tmp_path: Path,
) -> None:
    gw, kc = _gateway(tmp_path, dry_run=False, options_enabled=True, intraday_enabled=False)
    out = _place(gw, symbol="NIFTY2692223700CE", product="MIS", exchange="NFO")
    assert out["status"] == "BLOCKED"
    assert "INTRADAY_ENABLED" in str(out["error"])
    assert kc.calls == []


def test_an_nrml_future_is_blocked_in_the_gateway_with_both_switches_on(tmp_path: Path) -> None:
    gw, kc = _gateway(tmp_path, dry_run=False, options_enabled=True, intraday_enabled=True)
    out = _place(gw, symbol="NIFTY26SEPFUT", product="NRML", exchange="NFO")
    assert out["status"] == "BLOCKED"
    assert "MIS" in str(out["error"])
    assert kc.calls == []


def test_an_nrml_option_is_still_refused_by_the_overnight_guard_first(tmp_path: Path) -> None:
    """``assert_not_overnight_option`` is unchanged and still the earlier, harder refusal."""
    gw, kc = _gateway(tmp_path, dry_run=False, options_enabled=True, intraday_enabled=True)
    out = _place(gw, symbol="NIFTY2692223700CE", product="NRML", exchange="NFO")
    assert out["status"] == "BLOCKED"
    assert "past the close" in str(out["error"])
    assert kc.calls == []


def test_an_nfo_mis_option_with_both_switches_reaches_only_the_dry_run_branch(
    tmp_path: Path,
) -> None:
    """The one admitted shape, under DRY_RUN: it passes the gate and is simulated, not sent.
    This is the path OP10's paper confirms take."""
    gw, kc = _gateway(tmp_path, dry_run=True, options_enabled=True, intraday_enabled=True)
    out = _place(gw, symbol="NIFTY2692223700CE", product="MIS", exchange="NFO")
    assert out["status"] != "BLOCKED", out
    assert kc.calls == []


def test_a_gtt_on_a_derivative_venue_is_refused_whatever_the_switches(tmp_path: Path) -> None:
    """A GTT's leg is CNC by construction (``place_gtt_stop``), and CNC is a carry product: with
    OP2 no switch opens a derivative GTT, future or option."""
    gw, kc = _gateway(tmp_path, dry_run=False, options_enabled=True, intraday_enabled=True)
    out = asyncio.run(
        gw.place_gtt_stop(
            symbol="NIFTY26SEPFUT",
            qty=65,
            trigger=89.0,
            last_price=100.0,
            exchange="NFO",
            tenant=TENANT,
            plan_tenant=TENANT,
        )
    )
    assert out["status"] == "BLOCKED"
    assert kc.calls == []
