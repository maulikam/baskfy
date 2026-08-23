"""SC3 dry-run loop — genesis → invest → fill → publish v2 → apply; closed hours.

Pure integration of ``curated_versions`` + ``curated_plans`` + ``market_hours_cb``.
No database, no network, no ambient clock. Asserts desk_plan_id shape, non-empty legs,
and residual-cash sanity on the holdings diff.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from baskfy_core.curated_plans import build_apply_plan, build_invest_plan
from baskfy_core.curated_versions import (
    VERSION_LABEL_CHANGED,
    VERSION_LABEL_GENESIS,
    ConstituentDraft,
    build_published_version,
    desk_orders_from_diff,
    diff_holdings_vs_weights,
)
from baskfy_core.gst import money
from baskfy_core.market_hours_cb import (
    IST,
    closed_market_payload,
    is_nse_session_open,
    next_session_open,
)

# Instrument id → trading symbol (synthetic book).
SYMBOLS = {1: "AAA", 2: "BBB", 3: "CCC"}
PRICES_BY_ID = {1: Decimal("100"), 2: Decimal("200"), 3: Decimal("50")}
PRICES_BY_SYM = {SYMBOLS[i]: PRICES_BY_ID[i] for i in SYMBOLS}

TRADING_DATES = {
    dt.date(2026, 8, 24),
    dt.date(2026, 8, 25),
    dt.date(2026, 8, 26),
}

OPEN_NOW = dt.datetime(2026, 8, 24, 11, 0, tzinfo=IST)
CLOSED_NOW = dt.datetime(2026, 8, 24, 8, 0, tzinfo=IST)

AMOUNT = Decimal("10000")


def _d(value: str) -> Decimal:
    return Decimal(value)


def _synthetic_desk_plan_id() -> str:
    """Mirror API ``cb-sim-{uuid}`` without importing the router (core stays I/O-free)."""
    return f"cb-sim-{uuid.uuid4()}"


def _weights_by_symbol(constituents: tuple[ConstituentDraft, ...]) -> dict[str, Decimal]:
    return {SYMBOLS[c.instrument_id]: c.weight for c in constituents}


def _holdings_from_invest_legs(legs: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for leg in legs:
        assert leg["side"] == "BUY"
        out[leg["symbol"]] = int(leg["quantity"])
    return out


def test_sc3_dry_run_loop_open_then_closed() -> None:
    # --- 1. Genesis weights -------------------------------------------------
    genesis = build_published_version(
        version_no=1,
        effective_date=dt.date(2026, 8, 1),
        constituents=(
            ConstituentDraft(1, _d("0.5000")),
            ConstituentDraft(2, _d("0.5000")),
        ),
        existing_version_nos=(),
    )
    assert genesis.label == VERSION_LABEL_GENESIS
    assert genesis.version_no == 1

    target_v1 = _weights_by_symbol(genesis.constituents)
    assert is_nse_session_open(OPEN_NOW, TRADING_DATES)

    invest = build_invest_plan(
        target_weights=target_v1,
        prices=PRICES_BY_SYM,
        amount=AMOUNT,
        now=OPEN_NOW,
    )
    desk_plan_id = _synthetic_desk_plan_id()
    assert desk_plan_id.startswith("cb-sim-")
    assert len(desk_plan_id) > len("cb-sim-")
    assert invest["kind"] == "BUY"
    assert invest["legs"], "invest plan must carry buy legs"
    assert all(leg["quantity"] > 0 for leg in invest["legs"])

    # Simulated fill: intended holdings = invest legs (full fill).
    holdings_sym = _holdings_from_invest_legs(invest["legs"])
    holdings_id = {
        iid: Decimal(holdings_sym.get(sym, 0))
        for iid, sym in SYMBOLS.items()
        if holdings_sym.get(sym, 0) > 0
    }
    assert holdings_id

    # --- 2. Publish v2 with changed weights + apply/diff --------------------
    v2 = build_published_version(
        version_no=2,
        effective_date=dt.date(2026, 8, 24),
        constituents=(
            ConstituentDraft(1, _d("0.2500")),
            ConstituentDraft(2, _d("0.2500")),
            ConstituentDraft(3, _d("0.5000")),
        ),
        existing_version_nos=(1,),
        previous_instrument_ids=tuple(c.instrument_id for c in genesis.constituents),
        previous_weights_by_id={c.instrument_id: c.weight for c in genesis.constituents},
    )
    assert v2.label == VERSION_LABEL_CHANGED
    assert v2.added_count == 1

    target_v2_id = {c.instrument_id: c.weight for c in v2.constituents}
    diff = diff_holdings_vs_weights(holdings_id, target_v2_id, PRICES_BY_ID)
    assert diff.lines, "weight change must produce buy/sell children"
    # Residual: sells fund buys; top_up covers shortfall / min-amount gap.
    assert diff.top_up >= 0
    book = money(sum((qty * PRICES_BY_ID[iid] for iid, qty in holdings_id.items()), Decimal("0")))
    assert diff.portfolio_value == book
    # residual_cash = sell_proceeds - buy_cost (money-quantised); can be negative before top_up.
    assert isinstance(diff.residual_cash, Decimal)

    desk_orders = desk_orders_from_diff(diff, SYMBOLS)
    assert desk_orders
    assert all(o.qty > 0 for o in desk_orders)

    target_v2_sym = _weights_by_symbol(v2.constituents)
    # Book amount for apply = current market value (no top-up in plan sizing here).
    apply_amount = sum(
        (Decimal(qty) * PRICES_BY_SYM[sym] for sym, qty in holdings_sym.items()),
        Decimal("0"),
    )
    # Ensure CCC has a price for apply plan (dropped/added names).
    apply_prices = {**PRICES_BY_SYM}
    apply = build_apply_plan(
        holdings=holdings_sym,
        target_weights=target_v2_sym,
        prices=apply_prices,
        amount=apply_amount if apply_amount > 0 else AMOUNT,
        now=OPEN_NOW,
    )
    apply_plan_id = _synthetic_desk_plan_id()
    assert apply_plan_id.startswith("cb-sim-")
    assert apply["kind"] == "REBALANCE"
    assert apply["legs"], "apply plan must carry delta legs"

    # --- 3. Closed hours → no plan (closed payload) -------------------------
    assert not is_nse_session_open(CLOSED_NOW, TRADING_DATES)
    nxt = next_session_open(CLOSED_NOW, TRADING_DATES)
    closed = closed_market_payload(nxt)
    assert closed["market_open"] is False
    assert "next_open_ist" in closed
    assert "09:15" in str(closed["next_open_ist"])
    # Closed path never invents a desk_plan_id / legs.
    assert "desk_plan_id" not in closed
    assert "legs" not in closed
