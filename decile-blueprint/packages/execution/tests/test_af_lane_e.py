"""AF lane E — gateway and risk regressions that fail on the pre-fix code."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
from pathlib import Path

from baskfy_execution import OrderGateway, ProductGates, RiskConfig, RiskManager, TenantIds
from baskfy_execution.gtt import DEFAULT_STOP_BAND, StopBand, band_finding, refuse_stop

TENANT = TenantIds(user_id=1, broker_account_id=1)


class RecordingKC:
    GTT_TYPE_SINGLE = "single"
    TRANSACTION_TYPE_SELL = "SELL"
    ORDER_TYPE_LIMIT = "LIMIT"
    PRODUCT_CNC = "CNC"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def place_order(self, **kw: object) -> str:
        self.calls.append(("place_order", dict(kw)))
        return "OID-1"

    def place_gtt(self, **kw: object) -> dict[str, int]:
        self.calls.append(("place_gtt", dict(kw)))
        return {"trigger_id": 99}

    def instruments(self, exchange: str) -> list[dict[str, object]]:
        return [{"tradingsymbol": "RELIANCE", "tick_size": 0.05, "exchange": exchange}]


def _gw(tmp_path: Path, **gate_kw: bool) -> tuple[OrderGateway, RecordingKC]:
    kc = RecordingKC()
    gates = {"dry_run": True, **gate_kw}
    return (
        OrderGateway(
            kc,
            RiskManager(RiskConfig()),
            gates=lambda: ProductGates(**gates),
            journal_path=str(tmp_path / "journal.jsonl"),
        ),
        kc,
    )


# --- 0.7 -------------------------------------------------------------------------------
def test_mcx_nrml_is_blocked_by_the_product_allow_list(tmp_path: Path) -> None:
    """Deny-list of NFO/BFO alone let MCX/NRML through every guard (AF 0.7)."""
    gw, kc = _gw(tmp_path, dry_run=False)
    out = asyncio.run(
        gw.place(
            symbol="CRUDEOIL",
            qty=1,
            side="BUY",
            product="NRML",
            order_type="LIMIT",
            price=100.0,
            exchange="MCX",
            gross_exposure=100.0,
            tenant=TENANT,
            plan_tenant=TENANT,
        )
    )
    assert out["status"] == "BLOCKED"
    assert "F&O/derivatives" in str(out["error"])
    assert kc.calls == []


def test_place_gtt_stop_blocks_mcx_without_options(tmp_path: Path) -> None:
    gw, kc = _gw(tmp_path, dry_run=False)
    out = asyncio.run(
        gw.place_gtt_stop(
            symbol="CRUDEOIL",
            qty=1,
            trigger=90.0,
            last_price=100.0,
            exchange="MCX",
            tenant=TENANT,
            plan_tenant=TENANT,
        )
    )
    assert out["status"] == "BLOCKED"
    assert kc.calls == []


# --- 3.4 -------------------------------------------------------------------------------
def test_sent_is_replayed_from_the_journal_on_construction(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    journal.write_text(
        json.dumps(
            {
                "event": "dry_run",
                "client_id": "PLAN:RELIANCE",
                "symbol": "RELIANCE",
                "ts": 1.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _, _ = _gw(tmp_path)
    # Reconstruct against the same journal so replay sees the prior dry_run.
    gw2 = OrderGateway(
        RecordingKC(),
        RiskManager(),
        gates=lambda: ProductGates(dry_run=True),
        journal_path=str(journal),
    )
    out = asyncio.run(
        gw2.place(
            symbol="RELIANCE",
            qty=1,
            side="BUY",
            product="CNC",
            order_type="LIMIT",
            price=100.0,
            exchange="NSE",
            client_id="PLAN:RELIANCE",
            gross_exposure=100.0,
            tenant=TENANT,
            plan_tenant=TENANT,
        )
    )
    assert out["status"] == "DUPLICATE"


def test_concurrent_identical_client_ids_reserve_once(tmp_path: Path) -> None:
    """Without a lock, both tasks can pass the membership check before either writes."""
    gw, kc = _gw(tmp_path, dry_run=False)
    # Slow the rate limiter so both tasks race past the check if unlocked.
    original = gw.limits.order_slot

    async def slow_slot() -> None:
        await asyncio.sleep(0.05)
        await original()

    gw.limits.order_slot = slow_slot

    async def both() -> list[str]:
        results = await asyncio.gather(
            gw.place(
                symbol="RELIANCE",
                qty=1,
                side="BUY",
                product="CNC",
                order_type="LIMIT",
                price=100.0,
                exchange="NSE",
                client_id="SAME:LEG",
                gross_exposure=100.0,
                tenant=TENANT,
                plan_tenant=TENANT,
            ),
            gw.place(
                symbol="RELIANCE",
                qty=1,
                side="BUY",
                product="CNC",
                order_type="LIMIT",
                price=100.0,
                exchange="NSE",
                client_id="SAME:LEG",
                gross_exposure=100.0,
                tenant=TENANT,
                plan_tenant=TENANT,
            ),
        )
        return [str(r["status"]) for r in results]

    statuses = asyncio.run(both())
    assert sorted(statuses) == ["DUPLICATE", "PLACED"]
    assert len([c for c in kc.calls if c[0] == "place_order"]) == 1


# --- 3.5 -------------------------------------------------------------------------------
def test_assert_tradeable_returns_blocked_not_raised(tmp_path: Path) -> None:
    gw, kc = _gw(tmp_path, dry_run=False)
    out = asyncio.run(
        gw.place(
            symbol="SGBJUN29",
            qty=1,
            side="SELL",
            product="CNC",
            order_type="LIMIT",
            price=7000.0,
            exchange="NSE",
            gross_exposure=7000.0,
            tenant=TENANT,
            plan_tenant=TENANT,
        )
    )
    assert out["status"] == "BLOCKED"
    assert kc.calls == []


# --- 3.6 -------------------------------------------------------------------------------
def test_nan_trigger_is_refused_by_refuse_stop() -> None:
    why = refuse_stop(symbol="X", qty=1, trigger=float("nan"), last_price=100.0)
    assert why and "finite" in why


def test_nan_price_is_blocked_in_place(tmp_path: Path) -> None:
    gw, kc = _gw(tmp_path, dry_run=False)
    out = asyncio.run(
        gw.place(
            symbol="RELIANCE",
            qty=1,
            side="BUY",
            product="CNC",
            order_type="LIMIT",
            price=float("nan"),
            exchange="NSE",
            gross_exposure=100.0,
            tenant=TENANT,
            plan_tenant=TENANT,
        )
    )
    assert out["status"] == "BLOCKED"
    assert kc.calls == []
    assert math.isnan(float("nan"))  # sanity: the old <= checks would have passed


def test_sleeve_band_does_not_journal_too_close() -> None:
    """Weekly 8-12% journals too_close; sleeve 0.5% floors must not (AF 3.9)."""
    sleeve = StopBand(min_pct=0.005, max_pct=0.15)
    assert band_finding(trigger=97.0, last_price=100.0, band=sleeve) == ""
    assert band_finding(trigger=97.0, last_price=100.0, band=DEFAULT_STOP_BAND) == "too_close"


# --- 3.7 -------------------------------------------------------------------------------
def test_gross_exposure_is_required_on_place() -> None:
    # Keyword-only without a default: inspect via the signature default sentinel.
    sig = inspect.signature(OrderGateway.place)
    assert sig.parameters["gross_exposure"].default is inspect.Parameter.empty


def test_position_cap_accumulates_across_orders(tmp_path: Path) -> None:
    risk = RiskManager(RiskConfig(max_position_value=1_500.0))
    ok, _ = risk.pre_order("AAA", 1_000.0, 1_000.0, side="BUY")
    assert ok
    ok, why = risk.pre_order("AAA", 600.0, 1_600.0, side="BUY")
    assert not ok
    assert "position value" in why


def test_risk_state_persists_and_reloads(tmp_path: Path) -> None:
    path = str(tmp_path / "risk.json")
    first = RiskManager(RiskConfig(max_orders_per_day=3), state_path=path)
    first.pre_order("AAA", 10.0, 10.0, side="BUY")
    first.kill("test")
    second = RiskManager(RiskConfig(max_orders_per_day=3), state_path=path)
    assert second.state.killed is True
    assert second.state.orders_today == 1
    assert second.state.position_value["AAA"] == 10.0
