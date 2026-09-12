"""P4.3 — the gateway refuses a cross-tenant order; it does not 500."""

from __future__ import annotations

import asyncio
from pathlib import Path

from baskfy_execution import OrderGateway, ProductGates, RiskManager, TenantIds


class ExplodingKC:
    def place_order(self, **_: object) -> str:
        raise AssertionError("a refused order must not reach the broker")


CALLER = TenantIds(user_id=1, broker_account_id=10)
OTHER = TenantIds(user_id=2, broker_account_id=20)


def _gateway(tmp_path: Path) -> OrderGateway:
    return OrderGateway(
        ExplodingKC(),
        RiskManager(),
        gates=lambda: ProductGates(dry_run=True),
        journal_path=str(tmp_path / "journal.jsonl"),
    )


def test_cross_tenant_order_is_blocked_not_raised(tmp_path: Path) -> None:
    gw = _gateway(tmp_path)
    out = asyncio.run(
        gw.place(
            symbol="RELIANCE",
            qty=1,
            side="BUY",
            product="CNC",
            order_type="LIMIT",
            price=100.0,
            exchange="NSE",
            gross_exposure=100.0,
            tenant=CALLER,
            plan_tenant=OTHER,
        )
    )
    assert out["status"] == "BLOCKED"
    assert "tenant mismatch" in str(out["error"])
    assert out.get("order_id") is None


def test_matching_tenant_still_dry_runs(tmp_path: Path) -> None:
    gw = _gateway(tmp_path)
    out = asyncio.run(
        gw.place(
            symbol="RELIANCE",
            qty=1,
            side="BUY",
            product="CNC",
            order_type="LIMIT",
            price=100.0,
            exchange="NSE",
            gross_exposure=100.0,
            tenant=CALLER,
            plan_tenant=CALLER,
        )
    )
    assert out["status"] == "DRY_RUN"
    assert str(out["order_id"]).startswith("DRY-")


def test_mismatch_does_not_call_the_broker_when_dry_run_is_off(tmp_path: Path) -> None:
    gw = OrderGateway(
        ExplodingKC(),
        RiskManager(),
        gates=lambda: ProductGates(dry_run=False),
        journal_path=str(tmp_path / "journal.jsonl"),
    )
    out = asyncio.run(
        gw.place(
            symbol="RELIANCE",
            qty=1,
            side="BUY",
            product="CNC",
            order_type="LIMIT",
            price=100.0,
            exchange="NSE",
            gross_exposure=100.0,
            tenant=CALLER,
            plan_tenant=OTHER,
        )
    )
    assert out["status"] == "BLOCKED"
