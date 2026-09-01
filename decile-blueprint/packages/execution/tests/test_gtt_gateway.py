"""GTT stops behind the gateway — leaf 1.2.1, closing non-negotiable 6's documented exception.

`CLAUDE.md` non-negotiable 6 carried a caveat from the desk's own wording:

    "GTT stops go through `kite_client.place_gtt_stop`, which carries its own guard — the
     gateway has no GTT method yet."

These tests are the executable half of closing it. They assert the SPEC — that a GTT traverses
the same four layers an order does, in the same order, and that the half of the operation which
*removes* protection is guarded at least as tightly as the half that creates it — not that the
implementation happens to do what it currently does.

Every test here uses a recording broker double and asserts on what did and did not reach it.
Nothing in this file can place a live order or a live GTT: the repo-wide `block_network` fixture
in `decile-blueprint/conftest.py` refuses every non-loopback socket for the whole session, and
the double is a plain Python object.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path

import pytest
from baskfy_execution import (
    OrderGateway,
    ProductGates,
    RiskConfig,
    RiskManager,
    TenantIds,
    UntouchableInstrumentError,
)
from baskfy_execution.gtt import (
    DRY_RUN_GTT,
    DRY_RUN_GTT_DELETE,
    GTT_DELETE_ERROR,
    GTT_DELETED,
    GTT_ERROR,
    GTT_PLACED,
    GTT_PLACED_STATUSES,
    StopBand,
)

CALLER = TenantIds(user_id=1, broker_account_id=10)
OTHER = TenantIds(user_id=2, broker_account_id=20)

#: A Rs 100 name with a 5-paisa tick and a Rs 2750 name whose tick is a whole rupee. OFSS is
#: the real scrip that taught the desk to snap: "Trigger price should be a multiple of tick
#: size 1.00" is what cost it its stop on the first live arming run.
INSTRUMENTS = [
    {"tradingsymbol": "RELIANCE", "tick_size": 0.05},
    {"tradingsymbol": "OFSS", "tick_size": 1.00},
    {"tradingsymbol": "SONACOMS", "tick_size": 0.10},
    {"tradingsymbol": "NOTICK"},
]


class SpyKC:
    """A broker double that records instead of refusing.

    Recording rather than exploding on purpose: the gateway converts broker exceptions into
    `GTT_ERROR`, so a double that raises would let a guard failure pass as a handled error.
    Asserting `kc.calls == []` proves the guard ran *before* the broker was reached, which is
    the claim non-negotiable 7 actually makes.
    """

    GTT_TYPE_SINGLE = "single"
    TRANSACTION_TYPE_SELL = "SELL"
    TRANSACTION_TYPE_BUY = "BUY"
    ORDER_TYPE_LIMIT = "LIMIT"
    PRODUCT_CNC = "CNC"

    def __init__(
        self,
        *,
        fail: str = "",
        trigger_id: int | None = 777,
        instruments_payload: list[dict[str, object]] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._fail = fail
        self._trigger_id = trigger_id
        self._instruments = INSTRUMENTS if instruments_payload is None else instruments_payload

    def _maybe_fail(self, name: str) -> None:
        if self._fail == name:
            raise RuntimeError(f"broker refused {name}")

    def instruments(self, exchange: str) -> list[dict[str, object]]:
        self.calls.append(("instruments", {"exchange": exchange}))
        self._maybe_fail("instruments")
        return list(self._instruments)

    def place_gtt(self, **params: object) -> dict[str, int]:
        self.calls.append(("place_gtt", dict(params)))
        self._maybe_fail("place_gtt")
        return {} if self._trigger_id is None else {"trigger_id": self._trigger_id}

    def delete_gtt(self, **params: object) -> dict[str, int]:
        self.calls.append(("delete_gtt", dict(params)))
        self._maybe_fail("delete_gtt")
        return {"trigger_id": self._trigger_id}

    def place_order(self, **params: object) -> str:
        self.calls.append(("place_order", dict(params)))
        self._maybe_fail("place_order")
        return "ORDER-1"


def make_gateway(
    tmp_path: Path,
    kc: SpyKC | None = None,
    *,
    dry_run: bool = False,
    options_enabled: bool = False,
    risk: RiskManager | None = None,
) -> tuple[OrderGateway, SpyKC]:
    broker = kc or SpyKC()
    gw = OrderGateway(
        broker,
        risk or RiskManager(),
        gates=lambda: ProductGates(dry_run=dry_run, options_enabled=options_enabled),
        journal_path=str(tmp_path / "journal.jsonl"),
    )
    return gw, broker


def arm(gw: OrderGateway, **kwargs: object) -> dict:
    payload: dict[str, object] = {
        "symbol": "RELIANCE",
        "qty": 10,
        "trigger": 89.0,
        "last_price": 100.0,
        "tenant": CALLER,
        "plan_tenant": CALLER,
    }
    payload.update(kwargs)
    return asyncio.run(gw.place_gtt_stop(**payload))


def cancel(gw: OrderGateway, **kwargs: object) -> dict:
    payload: dict[str, object] = {
        "gtt_id": 4242,
        "symbol": "RELIANCE",
        "tenant": CALLER,
        "plan_tenant": CALLER,
    }
    payload.update(kwargs)
    return asyncio.run(gw.delete_gtt(**payload))


def journal(tmp_path: Path) -> list[dict[str, object]]:
    path = tmp_path / "journal.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def events(tmp_path: Path) -> list[str]:
    return [str(rec.get("event")) for rec in journal(tmp_path)]


# =====================================================================================
# The gateway is the path — a GTT method exists and both halves live on it (G1)
# =====================================================================================
def test_the_gateway_can_create_and_destroy_a_gtt() -> None:
    """Both halves are gateway methods. A GTT that can be removed off-gateway is the same
    hole as one that can be created off-gateway, pointing the other way."""
    assert callable(OrderGateway.place_gtt_stop)
    assert callable(OrderGateway.delete_gtt)


def test_the_gtt_layers_appear_in_the_same_order_as_the_order_path() -> None:
    """guards -> risk -> idempotency -> rate limits. Reordering them is how an untouchable
    instrument reaches a network call."""
    src = inspect.getsource(OrderGateway.place_gtt_stop)
    marks = [
        "GTT layer 1: untouchables",
        "GTT layer 1b",
        "GTT layer 2: risk",
        "GTT layer 3: idempotency",
        "GTT layer 4: rate limits",
    ]
    positions = [src.index(m) for m in marks]
    assert positions == sorted(positions), "the GTT layers are no longer in order"


# =====================================================================================
# Non-negotiable 7 — untouchables, before any network call (G2, G5)
# =====================================================================================
@pytest.mark.parametrize("symbol", ["SGBDE31III", "SGBDE31III-GB", "SGBJUN29"])
def test_an_untouchable_symbol_is_refused_a_gtt_before_the_broker_is_reached(
    symbol: str, tmp_path: Path
) -> None:
    """`EXCLUDED_SYMBOLS` holds "SGBDE31III" while the holding was "SGBDE31III-GB" — the guard
    is prefix- and series-aware, not an exact-match set."""
    gw, kc = make_gateway(tmp_path)
    with pytest.raises(UntouchableInstrumentError):
        arm(gw, symbol=symbol)
    assert kc.calls == [], "the broker was reached for an untouchable instrument"


@pytest.mark.parametrize("series", ["GB", "GS"])
def test_a_g_sec_series_is_refused_a_gtt_before_the_broker_is_reached(
    series: str, tmp_path: Path
) -> None:
    gw, kc = make_gateway(tmp_path)
    with pytest.raises(UntouchableInstrumentError):
        arm(gw, symbol="SOMEBOND", series=series)
    assert kc.calls == []


@pytest.mark.parametrize("symbol", ["SGBDE31III", "SGBDE31III-GB", "SGBJUN29"])
def test_an_untouchable_gtt_cannot_be_cancelled_around_the_guard_either(
    symbol: str, tmp_path: Path
) -> None:
    gw, kc = make_gateway(tmp_path)
    with pytest.raises(UntouchableInstrumentError):
        cancel(gw, symbol=symbol)
    assert kc.calls == []


def test_a_cancel_without_a_symbol_is_refused_rather_than_run_unguarded(tmp_path: Path) -> None:
    """The desk's `delete_gtt(gtt_id, symbol="")` ran no guard at all when the caller omitted
    the symbol. An opt-in guard is not a guard."""
    gw, kc = make_gateway(tmp_path)
    out = cancel(gw, symbol="")
    assert out["status"] == "BLOCKED"
    assert "without naming its instrument" in str(out["error"])
    assert kc.calls == []
    assert "gtt_delete_block" in events(tmp_path)


# =====================================================================================
# Non-negotiable 5 / P4.3 — product gates and tenancy apply to a GTT too (G2)
# =====================================================================================
def test_a_cross_tenant_gtt_is_blocked_not_raised(tmp_path: Path) -> None:
    gw, kc = make_gateway(tmp_path)
    out = arm(gw, plan_tenant=OTHER)
    assert out["status"] == "BLOCKED"
    assert "tenant mismatch" in str(out["error"])
    assert kc.calls == []


def test_a_cross_tenant_cancel_is_blocked_not_raised(tmp_path: Path) -> None:
    gw, kc = make_gateway(tmp_path)
    out = cancel(gw, plan_tenant=OTHER)
    assert out["status"] == "BLOCKED"
    assert "tenant mismatch" in str(out["error"])
    assert kc.calls == []


def test_an_fno_gtt_is_blocked_while_options_are_disabled(tmp_path: Path) -> None:
    """A futures symbol, deliberately: an OPTION on NFO is refused one layer earlier by the
    overnight guard (below), which would hide whether the F&O switch is doing anything at all."""
    gw, kc = make_gateway(tmp_path, options_enabled=False)
    out = arm(gw, symbol="NIFTY25SEPFUT", exchange="NFO")
    assert out["status"] == "BLOCKED"
    assert "F&O disabled" in str(out["error"])
    assert kc.calls == []


def test_an_option_gtt_is_refused_the_stricter_way_while_options_are_disabled(
    tmp_path: Path,
) -> None:
    """Two rules refuse it and the earlier, harder one wins — same order as `place()`, where
    the overnight-option guard sits above the product switches."""
    gw, kc = make_gateway(tmp_path, options_enabled=False)
    out = arm(gw, symbol="NIFTY25SEP24000CE", exchange="NFO")
    assert out["status"] == "BLOCKED"
    assert "past the close" in str(out["error"])
    assert kc.calls == []


def test_an_option_gtt_is_an_overnight_option_by_construction(tmp_path: Path) -> None:
    """A GTT rests for up to a year, so its CNC leg can only fire on a session this system
    never intended to be holding an option. Enabling options must not open that door."""
    gw, kc = make_gateway(tmp_path, options_enabled=True)
    out = arm(gw, symbol="NIFTY25SEP24000CE", exchange="NFO")
    assert out["status"] == "BLOCKED"
    assert "past the close" in str(out["error"])
    assert kc.calls == []
    assert "gtt_overnight_option_block" in events(tmp_path)


# =====================================================================================
# Non-negotiable 1 — DRY_RUN simulates end to end and places nothing (G7)
# =====================================================================================
def test_dry_run_arms_nothing_and_still_answers(tmp_path: Path) -> None:
    gw, kc = make_gateway(tmp_path, dry_run=True)
    out = arm(gw)
    assert out["status"] == DRY_RUN_GTT
    assert out["status"] in GTT_PLACED_STATUSES
    assert out["trigger"] == 89.0
    assert out["qty"] == 10
    assert kc.calls == [], "DRY_RUN reached the broker"
    assert "gtt_dry_run" in events(tmp_path)


def test_dry_run_cancels_nothing_and_still_answers(tmp_path: Path) -> None:
    gw, kc = make_gateway(tmp_path, dry_run=True)
    out = cancel(gw)
    assert out["status"] == DRY_RUN_GTT_DELETE
    assert kc.calls == []
    assert "gtt_dry_run_delete" in events(tmp_path)


def test_dry_run_makes_no_instrument_call_either(tmp_path: Path) -> None:
    """Snapping needs the instrument dump, which is a network call. A dry run reports the
    unsnapped trigger rather than making it — the desk's own shape."""
    gw, kc = make_gateway(tmp_path, dry_run=True)
    arm(gw, symbol="OFSS", trigger=2515.1, last_price=2750.0)
    assert [name for name, _ in kc.calls] == []


# =====================================================================================
# The port itself — the trigger the desk would have sent (G2)
# =====================================================================================
def test_a_live_stop_is_a_cnc_sell_limit_just_under_a_tick_snapped_trigger(
    tmp_path: Path,
) -> None:
    gw, kc = make_gateway(tmp_path)
    out = arm(gw, symbol="RELIANCE", qty=10, trigger=89.0, last_price=100.0)

    assert out["status"] == GTT_PLACED
    assert out["gtt_id"] == 777
    ((name, params),) = [c for c in kc.calls if c[0] == "place_gtt"]
    assert name == "place_gtt"
    assert params["trigger_type"] == SpyKC.GTT_TYPE_SINGLE
    assert params["tradingsymbol"] == "RELIANCE"
    assert params["exchange"] == "NSE"
    assert params["trigger_values"] == [89.0]
    assert params["last_price"] == 100.0
    (leg,) = params["orders"]
    assert leg["transaction_type"] == SpyKC.TRANSACTION_TYPE_SELL
    assert leg["order_type"] == SpyKC.ORDER_TYPE_LIMIT
    assert leg["product"] == SpyKC.PRODUCT_CNC, "a stop must never be MIS or NRML"
    assert leg["quantity"] == 10
    # 89.0 * 0.995 = 88.555, snapped to the 0.05 tick.
    assert leg["price"] == 88.55
    assert leg["price"] < out["trigger"], "the limit must sit under the trigger"


def test_a_coarse_tick_scrip_is_snapped_to_whole_rupees(tmp_path: Path) -> None:
    """OFSS trades in Rs 1.00 steps; a price that is not a multiple of its tick is rejected
    outright by Kite."""
    gw, kc = make_gateway(tmp_path)
    out = arm(gw, symbol="OFSS", qty=3, trigger=2515.1, last_price=2750.0)
    assert out["status"] == GTT_PLACED
    assert out["trigger"] == 2515.0
    assert out["limit"] == 2502.0  # 2515 * 0.995 = 2502.425 -> 2502
    ((_, params),) = [c for c in kc.calls if c[0] == "place_gtt"]
    assert params["trigger_values"] == [2515.0]


def test_an_instrument_with_no_tick_size_falls_back_to_five_paise(tmp_path: Path) -> None:
    gw, _ = make_gateway(tmp_path)
    out = arm(gw, symbol="NOTICK", trigger=89.03, last_price=100.0)
    assert out["trigger"] == 89.05


def test_the_instrument_dump_is_fetched_once_per_exchange(tmp_path: Path) -> None:
    gw, kc = make_gateway(tmp_path)
    arm(gw, symbol="RELIANCE", client_id="a")
    arm(gw, symbol="SONACOMS", client_id="b")
    assert [name for name, _ in kc.calls].count("instruments") == 1


# =====================================================================================
# The check the desk never had: is this actually a stop? (G2)
# =====================================================================================
@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"qty": 0}, "quantity must be positive"),
        ({"qty": -5}, "quantity must be positive"),
        ({"trigger": 0.0}, "trigger must be positive"),
        ({"trigger": -1.0}, "trigger must be positive"),
        ({"last_price": 0.0}, "cannot be checked against anything"),
        ({"trigger": 100.0}, "at or above the last price"),
        ({"trigger": 120.0}, "at or above the last price"),
    ],
)
def test_a_trigger_that_is_not_a_stop_never_reaches_the_broker(
    kwargs: dict[str, object], why: str, tmp_path: Path
) -> None:
    """A sell trigger at or above the last price fires on the next tick and liquidates the
    position — a market exit dressed as protection."""
    gw, kc = make_gateway(tmp_path)
    out = arm(gw, **kwargs)
    assert out["status"] == "BLOCKED"
    assert why in str(out["error"])
    assert kc.calls == []
    assert "gtt_block" in events(tmp_path)


def test_a_trigger_that_snaps_up_through_the_last_price_is_refused_after_snapping(
    tmp_path: Path,
) -> None:
    """Snapping rounds to the NEAREST tick, so on OFSS's Rs 1.00 tick a 2749.6 trigger becomes
    2750.0 — at the last price, where it fires immediately. The desk snapped and placed."""
    gw, kc = make_gateway(tmp_path)
    out = arm(gw, symbol="OFSS", trigger=2749.6, last_price=2750.0)
    assert out["status"] == "BLOCKED"
    assert "at or above the last price" in str(out["error"])
    assert [name for name, _ in kc.calls] == ["instruments"], "the GTT was still placed"
    snapped = [r for r in journal(tmp_path) if r.get("stage") == "after_tick_snap"]
    assert snapped and snapped[0]["trigger"] == 2750.0
    assert snapped[0]["requested_trigger"] == 2749.6


# =====================================================================================
# The 8-12% band: reported, never refused (G3 companion)
# =====================================================================================
def test_a_stop_inside_the_band_raises_no_finding(tmp_path: Path) -> None:
    gw, _ = make_gateway(tmp_path)
    arm(gw, trigger=89.0, last_price=100.0)  # 11% below — mid-band
    assert "gtt_band_warning" not in events(tmp_path)


@pytest.mark.parametrize(
    ("trigger", "finding"),
    [(99.5, "too_close"), (80.0, "too_far")],
)
def test_a_stop_outside_the_band_is_journalled_and_still_armed(
    trigger: float, finding: str, tmp_path: Path
) -> None:
    """Refusing to arm a stop because it is 12.4% below rather than 12.0% leaves a real
    position naked. The desk's own review reports these and never blocks on them."""
    gw, kc = make_gateway(tmp_path)
    out = arm(gw, trigger=trigger, last_price=100.0)
    assert out["status"] == GTT_PLACED
    assert any(c[0] == "place_gtt" for c in kc.calls)
    warnings = [r for r in journal(tmp_path) if r.get("event") == "gtt_band_warning"]
    assert warnings and warnings[0]["finding"] == finding


def test_the_band_boundaries_are_inside_the_band(tmp_path: Path) -> None:
    """`protection.py` compares with a 1e-9 slack, so a stop exactly on the boundary is not a
    finding. Preserved so the gateway and the review page cannot disagree about one stop."""
    gw, _ = make_gateway(tmp_path)
    arm(gw, trigger=92.0, last_price=100.0, client_id="min")  # exactly 8%
    arm(gw, trigger=88.0, last_price=100.0, client_id="max")  # exactly 12%
    assert "gtt_band_warning" not in events(tmp_path)


def test_the_band_is_injectable_and_refuses_an_inverted_one() -> None:
    assert StopBand().min_pct == 0.08
    assert StopBand().max_pct == 0.12
    with pytest.raises(ValueError, match="inverted or out of range"):
        StopBand(min_pct=0.2, max_pct=0.1)


# =====================================================================================
# Risk: advisory for arming, binding for cancelling (G2)
# =====================================================================================
def test_a_tripped_kill_switch_does_not_leave_a_position_unstopped(tmp_path: Path) -> None:
    """The day the daily-loss cap trips is exactly the day an unstopped book is most dangerous.
    `risk.pre_order` refuses everything once killed, so arming reads the switch instead of
    consulting it — and records that it did."""
    risk = RiskManager(RiskConfig())
    risk.on_pnl(-500_000.0)
    assert risk.state.killed
    gw, kc = make_gateway(tmp_path, risk=risk)

    out = arm(gw)
    assert out["status"] == GTT_PLACED
    assert any(c[0] == "place_gtt" for c in kc.calls)
    notes = [r for r in journal(tmp_path) if r.get("event") == "gtt_risk_note"]
    assert notes and "KILL SWITCH" in str(notes[0]["why"])


def test_a_tripped_kill_switch_refuses_to_remove_protection(tmp_path: Path) -> None:
    """Cancelling is the risk-increasing half. Refusing leaves the stop in place, which is the
    safe direction to fail in."""
    risk = RiskManager(RiskConfig())
    risk.on_pnl(-500_000.0)
    gw, kc = make_gateway(tmp_path, risk=risk)

    out = cancel(gw)
    assert out["status"] == "RISK_BLOCKED"
    assert "KILL SWITCH" in str(out["error"])
    assert kc.calls == []
    assert "gtt_delete_risk_block" in events(tmp_path)


def test_arming_a_stop_does_not_spend_the_days_order_budget(tmp_path: Path) -> None:
    """A GTT is not an order until it fires. Charging the order cap for stops would let a
    morning of arming refuse a real afternoon sell."""
    risk = RiskManager(RiskConfig())
    gw, _ = make_gateway(tmp_path, risk=risk)
    for n in range(5):
        arm(gw, client_id=f"c{n}")
    assert risk.state.orders_today == 0


# =====================================================================================
# Idempotency: two stops on one position is the failure, not one (G2)
# =====================================================================================
def test_a_replayed_stop_is_not_armed_twice(tmp_path: Path) -> None:
    """Two triggers on one position sell twice what is held when they fire — short delivery
    and an auction penalty. That is `protection.EXCESS`, found live on 18 Aug 2026."""
    gw, kc = make_gateway(tmp_path)
    first = arm(gw, client_id="PLAN1:RELIANCE")
    again = arm(gw, client_id="PLAN1:RELIANCE")
    assert first["status"] == GTT_PLACED
    assert again["status"] == "DUPLICATE"
    assert again["gtt_id"] == first["gtt_id"]
    assert [name for name, _ in kc.calls].count("place_gtt") == 1


def test_an_order_and_its_stop_do_not_share_an_idempotency_namespace(tmp_path: Path) -> None:
    """The desk builds client ids as `plan_id:symbol` for orders. If a stop shared that map it
    would be reported DUPLICATE of the buy that created it — a silently skipped stop, which is
    the one failure this whole path exists to prevent."""
    gw, kc = make_gateway(tmp_path)
    placed = asyncio.run(
        gw.place(
            symbol="RELIANCE",
            qty=10,
            side="BUY",
            product="CNC",
            order_type="LIMIT",
            price=100.0,
            exchange="NSE",
            client_id="PLAN1:RELIANCE",
            tenant=CALLER,
            plan_tenant=CALLER,
        )
    )
    assert placed["status"] == "PLACED"
    stop = arm(gw, client_id="PLAN1:RELIANCE")
    assert stop["status"] == GTT_PLACED
    assert [name for name, _ in kc.calls].count("place_gtt") == 1


def test_a_dry_run_stop_is_also_deduplicated(tmp_path: Path) -> None:
    gw, _ = make_gateway(tmp_path, dry_run=True)
    assert arm(gw, client_id="P:X")["status"] == DRY_RUN_GTT
    assert arm(gw, client_id="P:X")["status"] == "DUPLICATE"


# =====================================================================================
# Rate limits: a batch of stops cannot outrun Kite's caps (G2)
# =====================================================================================
def test_every_gtt_takes_a_rate_limit_slot(tmp_path: Path) -> None:
    gw, _ = make_gateway(tmp_path)
    taken = 0
    original = gw.limits.api_slot

    async def counting() -> None:
        nonlocal taken
        taken += 1
        await original()

    gw.limits.api_slot = counting
    arm(gw, client_id="a")
    # One slot for the GTT, one for the cold instrument dump.
    assert taken == 2
    cancel(gw)
    assert taken == 3


def test_a_refused_gtt_spends_no_rate_limit_slot(tmp_path: Path) -> None:
    """A refusal decided before the broker is reached must not consume the budget a real stop
    needs — the 18 Aug 2026 rate-limit incident was 21 orders against a cap of ten."""
    gw, _ = make_gateway(tmp_path)
    taken = 0
    original = gw.limits.api_slot

    async def counting() -> None:
        nonlocal taken
        taken += 1
        await original()

    gw.limits.api_slot = counting
    with pytest.raises(UntouchableInstrumentError):
        arm(gw, symbol="SGBJUN29")
    arm(gw, trigger=200.0, last_price=100.0)
    assert taken == 0


# =====================================================================================
# Cancelling, and what happens when the broker says no (G2)
# =====================================================================================
def test_a_cancel_reaches_the_broker_with_an_integer_trigger_id(tmp_path: Path) -> None:
    gw, kc = make_gateway(tmp_path)
    out = cancel(gw, gtt_id=4242)
    assert out["status"] == GTT_DELETED
    assert out["gtt_id"] == 4242
    ((_, params),) = [c for c in kc.calls if c[0] == "delete_gtt"]
    assert params == {"trigger_id": 4242}
    assert isinstance(params["trigger_id"], int)


def test_a_broker_refusal_to_cancel_is_reported_not_swallowed(tmp_path: Path) -> None:
    gw, _ = make_gateway(tmp_path, SpyKC(fail="delete_gtt"))
    out = cancel(gw)
    assert out["status"] == GTT_DELETE_ERROR
    assert "broker refused delete_gtt" in str(out["error"])
    assert "gtt_delete_error" in events(tmp_path)


def test_a_broker_refusal_to_arm_is_reported_not_swallowed(tmp_path: Path) -> None:
    gw, _ = make_gateway(tmp_path, SpyKC(fail="place_gtt"))
    out = arm(gw)
    assert out["status"] == GTT_ERROR
    assert "broker refused place_gtt" in str(out["error"])
    assert out["status"] not in GTT_PLACED_STATUSES
    errors = [r for r in journal(tmp_path) if r.get("event") == "gtt_error"]
    assert errors and errors[0]["stage"] == "place_gtt"


def test_a_failed_instrument_dump_does_not_place_an_unsnapped_trigger(tmp_path: Path) -> None:
    """Without a tick the trigger cannot be snapped, and an unsnapped trigger is rejected by
    Kite anyway. Reported as an error rather than guessed at."""
    gw, kc = make_gateway(tmp_path, SpyKC(fail="instruments"))
    out = arm(gw)
    assert out["status"] == GTT_ERROR
    assert [name for name, _ in kc.calls] == ["instruments"]
    errors = [r for r in journal(tmp_path) if r.get("event") == "gtt_error"]
    assert errors and errors[0]["stage"] == "tick_size"


# =====================================================================================
# The journal: every outcome leaves a record (G2)
# =====================================================================================
def test_every_terminal_gtt_outcome_is_journalled(tmp_path: Path) -> None:
    """The desk logged an armed stop into a redirect message and a JSON file written by the
    route. The gateway's journal is the record that survives the route being rewritten."""
    gw, _ = make_gateway(tmp_path)
    arm(gw, client_id="ok")
    cancel(gw)
    arm(gw, symbol="OFSS", trigger=2749.6, last_price=2750.0, client_id="bad")
    seen = set(events(tmp_path))
    assert {"gtt_placed", "gtt_deleted", "gtt_block"} <= seen


def test_the_placed_record_carries_what_slippage_and_coverage_are_measured_from(
    tmp_path: Path,
) -> None:
    gw, _ = make_gateway(tmp_path)
    arm(gw, symbol="RELIANCE", qty=10, trigger=89.0, last_price=100.0)
    (placed,) = [r for r in journal(tmp_path) if r.get("event") == "gtt_placed"]
    assert placed["symbol"] == "RELIANCE"
    assert placed["gtt_id"] == 777
    assert placed["qty"] == 10
    assert placed["trigger"] == 89.0
    assert placed["limit"] == 88.55
    assert placed["last_price"] == 100.0
    assert placed["drop_pct"] == 11.0
    assert "ts" in placed


# =====================================================================================
# The status vocabulary the desk's own route matches on (G2)
# =====================================================================================
def test_the_status_strings_are_the_ones_the_desk_route_already_matches_on() -> None:
    """`app/main.py:665` decides "armed" by `{"GTT_PLACED", "DRY_RUN_GTT"}` and its cancel
    line by `{"GTT_DELETED", "DRY_RUN_GTT_DELETE"}`. Those literals were written after a live
    run reported "0 armed, 17 failed" while sixteen triggers were resting at the exchange,
    because a hand-written success set had drifted from what the client returned. A false
    failure invites a re-arm, which is how a position ends up with two stops."""
    assert GTT_PLACED == "GTT_PLACED"
    assert DRY_RUN_GTT == "DRY_RUN_GTT"
    assert GTT_ERROR == "GTT_ERROR"
    assert GTT_DELETED == "GTT_DELETED"
    assert DRY_RUN_GTT_DELETE == "DRY_RUN_GTT_DELETE"
    assert GTT_DELETE_ERROR == "GTT_DELETE_ERROR"


def test_an_unnameable_but_created_trigger_is_reported_as_placed(tmp_path: Path) -> None:
    """A broker call that SUCCEEDED but answered in an unexpected shape must not be reported as
    a failure. The trigger exists; calling it an error invites a re-arm, and two triggers on one
    position sell shares that are not held when they fire."""
    gw, kc = make_gateway(tmp_path, SpyKC(trigger_id=None))
    out = arm(gw, client_id="P:X")
    assert out["status"] == GTT_PLACED
    assert out["gtt_id"] is None
    (placed,) = [r for r in journal(tmp_path) if r.get("event") == "gtt_placed"]
    assert "cannot be addressed" in str(placed["warning"])
    # And it is still deduplicated, so nothing re-arms it by accident.
    assert arm(gw, client_id="P:X")["status"] == "DUPLICATE"
    assert [name for name, _ in kc.calls].count("place_gtt") == 1


def test_a_broker_refusal_to_arm_does_not_claim_the_trigger_is_absent(tmp_path: Path) -> None:
    """Unlike an order, a GTT refusal carries no exception taxonomy to read the outcome from.
    "Unknown" is the honest answer, and it is the one that sends the operator to the GTT book
    instead of straight to a retry."""
    gw, _ = make_gateway(tmp_path, SpyKC(fail="place_gtt"))
    out = arm(gw)
    assert out["status"] == GTT_ERROR
    assert out["reached_exchange"] is None


def test_an_empty_instrument_dump_is_an_error_and_is_not_cached(tmp_path: Path) -> None:
    """Remembering an empty dump would mean "every symbol ticks at 5 paise" for the life of the
    process, and every stop armed afterwards snapped to the wrong grid — silently."""
    kc = SpyKC(instruments_payload=[])
    gw, _ = make_gateway(tmp_path, kc)
    first = arm(gw, client_id="a")
    assert first["status"] == GTT_ERROR
    assert "came back empty" in str(first["error"])
    second = arm(gw, client_id="b")
    assert second["status"] == GTT_ERROR
    assert [name for name, _ in kc.calls].count("instruments") == 2, "the empty dump was cached"


@pytest.mark.parametrize("bad", [0, -1, "4242", 42.0, True, None])
def test_a_malformed_trigger_id_never_reaches_the_broker(bad: object, tmp_path: Path) -> None:
    """A trigger id is an exchange handle, not a hint. Coercing one is how a cancel lands on
    some other live stop."""
    gw, kc = make_gateway(tmp_path)
    out = cancel(gw, gtt_id=bad)
    assert out["status"] == "BLOCKED"
    assert "is not a GTT trigger id" in str(out["error"])
    assert kc.calls == []
