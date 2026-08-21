"""No option position is ever held past the close.

Policy decision of 16 Aug 2026. A short option gaps against you overnight with no stop that
can fire while the market is shut, and on an expiry eve it also carries an ELM of 2% of
contract value per short leg. These tests pin the rule at the layer that enforces it, so it
holds even if the options program is switched on later.
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest

from app.core.guards import (
    OvernightOptionError,
    assert_not_overnight_option,
    is_option,
)


def place(gw, **kw):
    kw.setdefault("qty", 50)
    kw.setdefault("side", "SELL")
    return asyncio.run(gw.place(symbol=kw.pop("symbol"), **kw))


@pytest.fixture
def gw(monkeypatch):
    """A gateway with every OTHER gate opened, so only the overnight rule can refuse."""
    from app.core.gateway import OrderGateway
    from app.core.risk import RiskManager
    from app import config as C
    monkeypatch.setattr(C, "OPTIONS_ENABLED", True)
    monkeypatch.setattr(C, "INTRADAY_ENABLED", True)
    monkeypatch.setattr(C, "DRY_RUN", True)

    class KC:
        def place_order(self, **kw):
            raise AssertionError("an order reached Kite that should have been refused")

    risk = RiskManager()
    monkeypatch.setattr(risk, "pre_order", lambda *a, **k: (True, ""))
    return OrderGateway(KC(), risk)


# =====================================================================================
# the rule itself
# =====================================================================================
@pytest.mark.parametrize("product", ["NRML", "CNC", "nrml", "cnc"])
def test_an_option_cannot_be_sold_under_a_product_that_carries(product):
    with pytest.raises(OvernightOptionError):
        assert_not_overnight_option("NIFTY2681824350CE", "NFO", product)


@pytest.mark.parametrize("symbol", ["NIFTY2681824350CE", "NIFTY2681824350PE",
                                    "BANKNIFTY26AUG55000PE"])
def test_the_rule_covers_calls_and_puts_on_any_underlying(symbol):
    with pytest.raises(OvernightOptionError):
        assert_not_overnight_option(symbol, "NFO", "NRML")


def test_an_option_may_still_be_traded_intraday():
    """MIS is squared off by the broker before the close, so it cannot become an overnight
    position. Blocking it too would ban options outright, which is not the decision."""
    assert_not_overnight_option("NIFTY2681824350CE", "NFO", "MIS")


def test_the_rule_blocks_buys_as_well_as_sells():
    """Deliberately wider than 'no selling overnight'. If a long could be opened under
    NRML, the sell that closes it would then be refused — a guard that traps a position is
    worse than the risk it prevents. Nothing can be opened, so nothing needs closing."""
    with pytest.raises(OvernightOptionError):
        assert_not_overnight_option("NIFTY2681824350CE", "NFO", "NRML")


def test_equity_delivery_is_untouched():
    """The rebalancer is CNC equity. A rule that caught it would stop the whole system."""
    assert_not_overnight_option("RELIANCE", "NSE", "CNC")
    assert_not_overnight_option("TITAN", "NSE", "CNC")


def test_a_future_is_not_an_option():
    """Futures carry their own risk and their own decision; this rule is about options and
    must not silently expand to cover something the user did not ask about."""
    assert_not_overnight_option("NIFTY26AUGFUT", "NFO", "NRML")


def test_symbols_ending_in_ce_or_pe_are_recognised_as_options():
    assert is_option("NIFTY2681824350CE") and is_option("NIFTY2681824350PE")
    # RELIANCE ends in CE and JUSTDIAL-style names end in letters too. Only the strike
    # digit separates a real contract from an equity whose name happens to end that way.
    assert not is_option("NIFTY26AUGFUT")
    assert not is_option("RELIANCE"), "an equity ending in CE is not a call option"
    assert is_option("RELIANCE26AUG1400CE")


# =====================================================================================
# enforcement, at the only layer that can reach Kite
# =====================================================================================
def test_the_gateway_refuses_an_overnight_option_even_with_options_enabled(gw):
    r = place(gw, symbol="NIFTY2681824350CE", exchange="NFO", product="NRML", price=126.5)
    assert r["status"] == "BLOCKED"
    assert "never holds an option overnight" in r["error"]


def test_the_refusal_survives_dry_run_being_off(gw, monkeypatch):
    """A guard that only holds in simulation is not a guard."""
    from app import config as C
    monkeypatch.setattr(C, "DRY_RUN", False)
    r = place(gw, symbol="NIFTY2681824350CE", exchange="NFO", product="NRML", price=126.5)
    assert r["status"] == "BLOCKED"          # KC.place_order would have raised


def test_the_refusal_comes_before_the_risk_manager_and_the_rate_limiter(gw):
    """Ordering matters: a blocked order must not consume a rate-limit slot or move any
    risk counter, or a refused strategy could still starve the real one."""
    calls = []
    gw.risk.pre_order = lambda *a, **k: (calls.append(a), (True, ""))[1]
    place(gw, symbol="NIFTY2681824350CE", exchange="NFO", product="NRML", price=126.5)
    assert calls == []


def test_an_intraday_option_still_passes_the_gateway(gw):
    r = place(gw, symbol="NIFTY2681824350CE", exchange="NFO", product="MIS", price=126.5)
    assert r["status"] == "DRY_RUN"


def test_the_block_is_journalled(gw, tmp_path, monkeypatch):
    """A refusal that leaves no trace cannot be audited after the fact."""
    import app.core.gateway as G
    log = tmp_path / "journal.jsonl"
    monkeypatch.setattr(G, "JOURNAL", str(log))
    place(gw, symbol="NIFTY2681824350CE", exchange="NFO", product="NRML", price=126.5)
    assert "overnight_option_block" in log.read_text()


# =====================================================================================
# the other path to Kite's order APIs
# =====================================================================================
def test_a_gtt_can_never_be_placed_on_an_option(monkeypatch):
    """A GTT rests at the exchange for up to a year, so an option GTT is an overnight
    option position by construction."""
    from app import config as C
    from app.kite_client import Kite
    monkeypatch.setattr(C, "DRY_RUN", False)
    k = Kite.__new__(Kite)
    with pytest.raises(OvernightOptionError):
        k.place_gtt_stop("NIFTY2681824350CE", 50, 100.0, 120.0, exchange="NFO")


def test_the_equity_gtt_path_is_unaffected(monkeypatch):
    from app import config as C
    from app.kite_client import Kite
    monkeypatch.setattr(C, "DRY_RUN", True)
    k = Kite.__new__(Kite)
    assert k.place_gtt_stop("TITAN", 10, 3120.0, 3400.0)["status"] == "DRY_RUN_GTT"


# =====================================================================================
# drift
# =====================================================================================
def test_no_strategy_declares_a_carry_product():
    """The planners are MIS-only by construction. If one ever declares NRML, the gateway
    would still refuse it — but the mismatch means someone intended otherwise."""
    for path in pathlib.Path("app/strategies").glob("*.py"):
        for line in path.read_text().splitlines():
            if "products = (" in line:
                assert "NRML" not in line, f"{path.name}: {line.strip()}"


def test_the_rule_is_not_a_config_flag():
    """A knob that can be turned is not a guarantee. Hard-coded, like the SGB list."""
    import app.config as C
    assert not [n for n in dir(C) if "OVERNIGHT" in n.upper()], \
        "the overnight-option rule must not be switchable from config"
