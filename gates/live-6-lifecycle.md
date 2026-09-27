# Gates: LV6 — one visible lifecycle per trade; adopt a manual holding; the trade card says its rule

Scope: entry to confirmed exit with no orphan position, no over-sized residual GTT and no unexplained discrepancy after a manual broker action — visible on the desk. A manually bought Kite holding is adopted explicitly, never claimed silently.

- [x] C1: /lifecycle lists every open position across swing, TWT and VBT with filled qty, open qty, stop state, stop qty, rupee risk, exit rule, next action and overdue flag
  CHECK: cd kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q tests/test_lifecycle.py -k 'rows or page' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 4 passed, 17 deselected, 2 warnings in 2.96s

- [x] C2: stop state is reconciled against the broker's GTT list — ARMED, GTT_MISSING, GTT_OVERSIZED (stop qty > open qty), TRIGGERED_UNFILLED (trigger fired, limit not filled) — a triggered-but-unfilled stop is an unresolved position
  CHECK: cd kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q tests/test_lifecycle.py -k 'stop_state' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 5 passed, 16 deselected, 1 warning in 3.14s

- [x] C3: POST /lifecycle/adopt with confirm=true creates the position in the named sleeve with the given cost and quantity, records lv_adoption, links an existing GTT or arms one through the gateway; without confirm nothing is written; an unknown sleeve or a held name is refused
  CHECK: cd kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q tests/test_lifecycle.py -k 'adopt' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 8 passed, 13 deselected, 1 warning in 2.95s

- [x] C4: each sleeve page's trade card shows entry, quantity, rupee risk, initial and current stop, and the exact exit rule (TWT 20% high-water trail; VBT close below 21-EMA; swing partial + MA trail)
  CHECK: cd kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q tests/test_lifecycle.py -k 'card' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 4 passed, 17 deselected, 1 warning in 3.32s

- [x] C5: the lifecycle page is read-only apart from adopt; no route on it places an order (asserted the way test_swing_readonly asserts)
  CHECK: cd kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q tests/test_lifecycle.py -k 'readonly or no_order' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 2 passed, 19 deselected, 1 warning in 3.19s

- [x] C6: desk suite green
  CHECK: cd kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q 2>&1 | tail -1
  EXPECT: /^\d+ passed(, \d+ skipped)?/
  EVIDENCE: 2506 passed, 147 skipped, 234 warnings, 12 subtests passed in 95.10s (0:01:35)
