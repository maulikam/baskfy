"""`modify_gtt_quantity` and `cancel_order` — the two gateway methods SW10.5 adds (STANDING-
ANSWERS A8), and the proof the weekly book is byte-for-byte unaffected.

A8: a live buy is a marketable LIMIT that can fill in part; the stop armed for the first fill
covers the first fill only, and when the rest arrives the trigger is **modified** to the new
quantity — never a second trigger beside the first. What has not filled by 10:45 is cancelled.
Both are order-shaped actions, and law 2 puts every one of those behind the gateway's layers:
tenant, untouchables, "is this a stop", the kill switch, the rate limit, the journal, and a
dry-run branch that answers without touching the broker.

The fixtures are `test_gtt_gateway.py`'s, reused rather than retyped.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest
from baskfy_execution.gateway import ORDER_CANCELLED_STATUSES, OrderGateway
from baskfy_execution.gtt import (
    DRY_RUN_GTT_MODIFY,
    GTT_MODIFIED,
    GTT_MODIFIED_STATUSES,
    GTT_MODIFY_ERROR,
)
from baskfy_execution.risk import RiskConfig, RiskManager
from baskfy_execution.tenancy import TenantIds
from test_gtt_gateway import CALLER, SpyKC, events, journal, make_gateway


class ModifyingKC(SpyKC):
    """`SpyKC` plus the two broker methods the new gateway methods reach."""

    def modify_gtt(self, **params: object) -> dict[str, int]:
        self.calls.append(("modify_gtt", dict(params)))
        self._maybe_fail("modify_gtt")
        return {"trigger_id": int(str(params["trigger_id"]))}

    def cancel_order(self, **params: object) -> str:
        self.calls.append(("cancel_order", dict(params)))
        self._maybe_fail("cancel_order")
        return str(params["order_id"])


def modify(gw: OrderGateway, **kwargs: object) -> dict:
    payload: dict[str, object] = {
        "gtt_id": 4242,
        "symbol": "RELIANCE",
        "qty": 60,
        "trigger": 89.0,
        "last_price": 100.0,
        "tenant": CALLER,
        "plan_tenant": CALLER,
    }
    payload.update(kwargs)
    return asyncio.run(gw.modify_gtt_quantity(**payload))


def cancel(gw: OrderGateway, **kwargs: object) -> dict:
    payload: dict[str, object] = {
        "order_id": "ORD-1",
        "symbol": "RELIANCE",
        "tenant": CALLER,
        "plan_tenant": CALLER,
    }
    payload.update(kwargs)
    return asyncio.run(gw.cancel_order(**payload))


def _gateway(
    tmp_path: Path, *, dry_run: bool = False, risk: RiskManager | None = None
) -> tuple[OrderGateway, ModifyingKC]:
    kc = ModifyingKC()
    gw, _ = make_gateway(tmp_path, kc, dry_run=dry_run, risk=risk)
    return gw, kc


# --- modify: the ONLY way to re-size a stop ----------------------------------------------


def test_a_live_modify_re_sizes_the_same_trigger_and_journals_it(tmp_path: Path) -> None:
    """The trigger is snapped, the limit sits at the swing cushion, the quantity is the new
    one, and Kite's `modify_gtt` is addressed by the resting trigger id — one call, one id."""
    gw, kc = _gateway(tmp_path)
    out = modify(gw, qty=60, limit_fraction=0.97)
    assert out["status"] == GTT_MODIFIED
    assert out["gtt_id"] == 4242 and out["qty"] == 60
    assert out["trigger"] == 89.0 and out["limit"] == 86.35
    ((_, params),) = [c for c in kc.calls if c[0] == "modify_gtt"]
    assert params["trigger_id"] == 4242
    orders = params["orders"]
    assert isinstance(orders, list)
    (leg,) = orders
    assert isinstance(leg, dict)
    assert leg["quantity"] == 60 and leg["transaction_type"] == "SELL"
    assert not any(c[0] == "place_gtt" for c in kc.calls), "never a second trigger"
    (modified,) = [r for r in journal(tmp_path) if r.get("event") == "gtt_modified"]
    assert modified["qty"] == 60 and modified["gtt_id"] == 4242


def test_dry_run_modify_touches_nothing_and_still_answers(tmp_path: Path) -> None:
    gw, kc = _gateway(tmp_path, dry_run=True)
    out = modify(gw, qty=60)
    assert out["status"] == DRY_RUN_GTT_MODIFY and out["qty"] == 60
    assert out["status"] in GTT_MODIFIED_STATUSES
    assert kc.calls == []
    assert events(tmp_path) == ["gtt_dry_run_modify"]


def test_an_untouchable_symbol_is_refused_a_modify_before_the_broker(tmp_path: Path) -> None:
    gw, kc = _gateway(tmp_path)
    out = modify(gw, symbol="SGBAUG28")
    assert out["status"] == "BLOCKED"
    assert "protected instrument" in str(out["error"])
    assert kc.calls == []


def test_a_cross_tenant_modify_is_blocked_not_raised(tmp_path: Path) -> None:
    gw, kc = _gateway(tmp_path)
    other = TenantIds(user_id=CALLER.user_id + 1, broker_account_id=CALLER.broker_account_id)
    out = modify(gw, plan_tenant=other)
    assert out["status"] == "BLOCKED" and kc.calls == []


def test_a_modify_to_zero_shares_is_refused_as_not_a_stop(tmp_path: Path) -> None:
    """Zero is a cancel by another name; `delete_gtt` is the way to say that."""
    gw, kc = _gateway(tmp_path)
    out = modify(gw, qty=0)
    assert out["status"] == "BLOCKED" and "delete_gtt" in out["error"]
    assert kc.calls == []


def test_a_malformed_trigger_id_is_refused_before_the_broker(tmp_path: Path) -> None:
    gw, kc = _gateway(tmp_path)
    for bad in ("4242", 0, -1, True, None):
        out = modify(gw, gtt_id=bad)
        assert out["status"] == "BLOCKED", bad
    assert kc.calls == []


def test_a_modify_whose_trigger_is_not_a_stop_is_refused(tmp_path: Path) -> None:
    gw, kc = _gateway(tmp_path)
    out = modify(gw, trigger=101.0, last_price=100.0)
    assert out["status"] == "BLOCKED" and kc.calls == []


def test_a_tripped_kill_switch_does_not_stop_a_stop_from_being_re_sized(tmp_path: Path) -> None:
    risk = RiskManager(RiskConfig())
    risk.on_pnl(-500_000.0)
    assert risk.state.killed
    gw, _ = _gateway(tmp_path, risk=risk)
    out = modify(gw, qty=60)
    assert out["status"] == GTT_MODIFIED
    assert "gtt_risk_note" in events(tmp_path)


def test_a_broker_failure_on_modify_is_reported_not_raised(tmp_path: Path) -> None:
    kc = ModifyingKC(fail="modify_gtt")
    gw, _ = make_gateway(tmp_path, kc)
    out = modify(gw)
    assert out["status"] == GTT_MODIFY_ERROR and out["reached_exchange"] is None


def test_modifying_twice_to_the_same_quantity_is_not_a_duplicate(tmp_path: Path) -> None:
    """A caller reconciling a fill must be able to retry."""
    gw, kc = _gateway(tmp_path)
    assert modify(gw, qty=60)["status"] == GTT_MODIFIED
    assert modify(gw, qty=60)["status"] == GTT_MODIFIED
    assert sum(1 for c in kc.calls if c[0] == "modify_gtt") == 2


def test_a_limit_fraction_outside_the_unit_interval_is_refused_before_anything(
    tmp_path: Path,
) -> None:
    gw, kc = _gateway(tmp_path)
    with pytest.raises(ValueError, match="limit_fraction"):
        modify(gw, limit_fraction=1.5)
    assert kc.calls == [] and journal(tmp_path) == []


# --- cancel: the ONLY way to cancel an order ---------------------------------------------


def test_a_live_cancel_reaches_the_broker_once_and_is_journalled(tmp_path: Path) -> None:
    gw, kc = _gateway(tmp_path)
    out = cancel(gw, order_id="ORD-7")
    assert out["status"] == "ORDER_CANCELLED" and out["status"] in ORDER_CANCELLED_STATUSES
    assert kc.calls == [("cancel_order", {"variety": "regular", "order_id": "ORD-7"})]
    assert events(tmp_path) == ["order_cancelled"]


def test_dry_run_cancel_touches_nothing_and_still_answers(tmp_path: Path) -> None:
    gw, kc = _gateway(tmp_path, dry_run=True)
    out = cancel(gw)
    assert out["status"] == "ORDER_CANCEL_DRY_RUN" and out["status"] in ORDER_CANCELLED_STATUSES
    assert kc.calls == [] and events(tmp_path) == ["order_cancel_dry_run"]


def test_an_untouchable_symbol_cannot_be_cancelled_around_the_guard(tmp_path: Path) -> None:
    gw, kc = _gateway(tmp_path)
    out = cancel(gw, symbol="SGBAUG28")
    assert out["status"] == "BLOCKED"
    assert "protected instrument" in str(out["error"])
    assert kc.calls == []


def test_a_cancel_without_a_symbol_or_an_order_id_is_refused(tmp_path: Path) -> None:
    gw, kc = _gateway(tmp_path)
    assert cancel(gw, symbol="")["status"] == "BLOCKED"
    assert cancel(gw, order_id="")["status"] == "BLOCKED"
    assert kc.calls == []


def test_a_cross_tenant_cancel_is_blocked_not_raised(tmp_path: Path) -> None:
    gw, kc = _gateway(tmp_path)
    other = TenantIds(user_id=CALLER.user_id + 1, broker_account_id=CALLER.broker_account_id)
    assert cancel(gw, plan_tenant=other)["status"] == "BLOCKED" and kc.calls == []


def test_a_broker_failure_on_cancel_is_reported_not_raised(tmp_path: Path) -> None:
    kc = ModifyingKC(fail="cancel_order")
    gw, _ = make_gateway(tmp_path, kc)
    out = cancel(gw)
    assert out["status"] == "ORDER_CANCEL_ERROR"
    assert events(tmp_path) == ["order_cancel_error"]


# --- the weekly book is untouched --------------------------------------------------------


def test_the_two_methods_are_additive_and_the_weekly_paths_are_unchanged() -> None:
    """Nothing in `place`, `place_gtt_stop` or `delete_gtt` reads the new methods; the weekly
    book's `protection.py` and `main.py` never call them. Additive, byte for byte."""
    for name in ("place", "place_gtt_stop", "delete_gtt"):
        source = inspect.getsource(getattr(OrderGateway, name))
        assert "modify_gtt" not in source and "cancel_order" not in source, name
    desk = Path(__file__).resolve().parents[3] / "kite-momentum-rebalancer" / "app"
    for file in ("protection.py", "main.py", "rebalance.py"):
        path = desk / file
        if path.exists():
            text = path.read_text()
            assert "modify_gtt_quantity" not in text and "cancel_order(" not in text, file
