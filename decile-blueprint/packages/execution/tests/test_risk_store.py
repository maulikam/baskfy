"""LV3 — one account, many processes: the risk manager over a shared, lockable state store.

`gates/live-3-risk.md`. Two managers over one store stand for two processes (the desk and
`twt-auto`, say); the store here is a dict behind a lock, the shape `PgRiskStateStore` gives it
over `risk_ledger`. Nothing here touches a broker; the JSON-file path is asserted unchanged.
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Iterator
from pathlib import Path

from baskfy_execution.gtt import kill_switch_reason
from baskfy_execution.risk import RiskConfig, RiskManager


class MemoryStore:
    """A shared row: a dict behind a re-entrant lock, counting how often it was held."""

    def __init__(self) -> None:
        self.row: dict[str, object] | None = None
        self._lock = threading.RLock()
        self.locks = 0
        self.saves = 0

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        with self._lock:
            self.locks += 1
            yield

    def load(self) -> dict[str, object] | None:
        return None if self.row is None else dict(self.row)

    def save(self, payload: dict[str, object]) -> None:
        self.saves += 1
        self.row = dict(payload)


def a_pair(store: MemoryStore, **cfg: float) -> tuple[RiskManager, RiskManager]:
    config = RiskConfig(**cfg) if cfg else RiskConfig()
    return RiskManager(config, store=store), RiskManager(config, store=store)


class TestTheStoreIsTheTruth:
    def test_pre_order_reloads_under_the_lock_and_saves_after(self) -> None:
        store = MemoryStore()
        manager = RiskManager(store=store)
        locks_before = store.locks
        ok, why = manager.pre_order("A", 1_000.0, 1_000.0)
        assert (ok, why) == (True, "ok")
        assert store.locks == locks_before + 1
        assert store.row is not None and store.row["orders_today"] == 1
        assert store.row["position_value"] == {"A": 1_000.0}

    def test_two_processes_cannot_spend_the_same_cap_twice(self) -> None:
        """A shared_cap test: the second manager sees the first's reservation before deciding."""
        store = MemoryStore()
        swing, twt = a_pair(store, max_position_value=10_000.0)
        assert swing.pre_order("SAME", 6_000.0, 6_000.0)[0]
        ok, why = twt.pre_order("SAME", 6_000.0, 6_000.0)
        assert not ok
        assert "SAME position value 12,000 > cap" in why
        # Per-process memory would have said yes: each alone is under the cap.
        alone = RiskManager(RiskConfig(max_position_value=10_000.0))
        assert alone.pre_order("SAME", 6_000.0, 6_000.0)[0]

    def test_the_order_counter_is_account_wide(self) -> None:
        store = MemoryStore()
        swing, twt = a_pair(store, max_orders_per_day=3)
        assert swing.pre_order("A", 1.0, 1.0)[0]
        assert twt.pre_order("B", 1.0, 1.0)[0]
        assert swing.pre_order("C", 1.0, 1.0)[0]
        ok, why = twt.pre_order("D", 1.0, 1.0)
        assert not ok and why == "own daily order cap reached"

    def test_release_gives_an_unfilled_reservation_back_to_every_process(self) -> None:
        store = MemoryStore()
        swing, twt = a_pair(store, max_position_value=10_000.0)
        assert swing.pre_order("X", 8_000.0, 8_000.0)[0]
        assert not twt.pre_order("X", 4_000.0, 4_000.0)[0]
        swing.release("X", 6_000.0)  # 6,000 of the 8,000 never filled
        ok, _ = twt.pre_order("X", 4_000.0, 4_000.0)
        assert ok
        assert store.row is not None and store.row["position_value"] == {"X": 6_000.0}

    def test_seed_positions_counts_a_manual_holding_toward_exposure(self) -> None:
        store = MemoryStore()
        swing, twt = a_pair(store, max_position_value=10_000.0)
        swing.seed_positions({"HELD": 9_500.0, "ZERO": 0.0})
        ok, why = twt.pre_order("HELD", 1_000.0, 1_000.0)
        assert not ok and "HELD position value 10,500 > cap" in why
        # A seed never lowers a reservation already taken this session.
        assert twt.pre_order("NEW", 7_000.0, 7_000.0)[0]
        swing.seed_positions({"NEW": 100.0})
        assert store.row is not None and store.row["position_value"]["NEW"] == 7_000.0

    def test_kill_in_one_process_is_seen_by_the_other(self) -> None:
        store = MemoryStore()
        swing, twt = a_pair(store)
        assert twt.pre_order("A", 1.0, 1.0)[0]
        swing.kill("operator")
        ok, why = twt.pre_order("B", 1.0, 1.0)
        assert not ok and why.startswith("KILL SWITCH: operator")
        # The advisory reader used by the GTT path refreshes too.
        assert kill_switch_reason(twt) == "KILL SWITCH: operator"

    def test_restart_preserves_the_day(self) -> None:
        store = MemoryStore()
        first = RiskManager(store=store)
        assert first.pre_order("A", 5.0, 5.0)[0]
        first.on_pnl(-12.5)
        del first
        again = RiskManager(store=store)
        assert again.state.orders_today == 1
        assert again.state.day_pnl == -12.5
        assert again.state.position_value == {"A": 5.0}


class TestTheOldPathsAreUnchanged:
    def test_the_json_file_path_still_works_without_a_store(self, tmp_path: Path) -> None:
        path = str(tmp_path / "risk.json")
        manager = RiskManager(state_path=path)
        assert manager.pre_order("A", 10.0, 10.0)[0]
        saved = json.loads(Path(path).read_text(encoding="utf-8"))
        assert saved["orders_today"] == 1 and saved["position_value"] == {"A": 10.0}
        assert RiskManager(state_path=path).state.orders_today == 1

    def test_without_a_store_or_a_path_the_state_is_in_memory_as_before(self) -> None:
        manager = RiskManager()
        assert manager.pre_order("A", 10.0, 10.0)[0]
        manager.refresh()  # a no-op without a store
        assert manager.state.orders_today == 1
        assert kill_switch_reason(manager) == ""
