# Gates: LV2 — live fill reconciliation and same-fill stop protection, all three sleeves

Scope: a desk-owned reconciler that reads the broker's order book, applies fill deltas idempotently to every sleeve, protects each filled quantity, surfaces protection issues, and refuses new entries while any is unresolved. The dry-run and the live path are one code path.

- [x] F1: `app/reconcile.py` exists with reconcile_once, protection_unresolved, the OrderBook and SleeveHooks protocols, and `python -m app.reconcile` runs one pass
  CHECK: cd kite-momentum-rebalancer && grep -cE '^(async )?def (reconcile_once|protection_unresolved|main)\b|^class (OrderBook|SleeveHooks)\b' app/reconcile.py
  EXPECT: 5
  EVIDENCE: 5

- [x] F2: TWT partial → complete: one position, fills summing to the order, ONE GTT resized to the open quantity (never a second GTT); duplicate and out-of-order updates change nothing
  CHECK: cd kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q tests/test_reconcile.py -k 'twt and (partial or duplicate or out_of_order)' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 3 passed, 18 deselected in 0.83s

- [x] F3: partial → cancel keeps the position for what filled and closes the line; nothing filled → EXPIRED/REJECTED with no position and no stop
  CHECK: cd kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q tests/test_reconcile.py -k 'cancel or rejected' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 2 passed, 19 deselected in 0.68s

- [x] F4: VBT real buys are booked only from the broker's fill; VBT `_sell_at_open`'s real path books nothing until the broker reports the fill, then the exit is reconciled at the broker's average for the filled quantity
  CHECK: cd kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q tests/test_reconcile.py tests/test_vbt_execute.py -k 'vbt' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 26 passed, 17 deselected in 1.29s

- [x] F5: restart recovery — a SENT order accepted before a restart is reconciled by the first pass; a fill after the poll window is picked up
  CHECK: cd kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q tests/test_reconcile.py -k 'restart or late_fill' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 3 passed, 18 deselected in 0.68s

- [x] F6: GTT rejection, a GTT missing from the broker's list, an over-sized residual GTT and a manual broker exit each write an lv_protection_issue; buys in every sleeve are refused PROTECTION_UNRESOLVED while one is open
  CHECK: cd kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q tests/test_reconcile.py -k 'issue or unresolved or external_exit' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 8 passed, 13 deselected, 4 warnings in 0.80s

- [x] F7: recorded fill quantity equals protected quantity in every scenario; zero duplicate buys, zero duplicate stops (property asserted over the scenario set)
  CHECK: cd kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q tests/test_reconcile.py -k 'invariant' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 1 passed, 20 deselected in 0.92s

- [x] F8: migration 0055 creates lv_protection_issue, lv_heartbeat, lv_adoption and risk_ledger in the desk schema and downgrades cleanly
  CHECK: cd decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest services/api/tests/test_migration_0055.py 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 3 passed in 9.85s

- [x] F9: every broker mutation in the new code goes through the gateway (no kc.place_order / place_gtt / modify_gtt / delete_gtt outside packages/execution and app/kite_client.py)
  CHECK: grep -rnE 'kc\.(place_order|place_gtt|modify_gtt|delete_gtt|cancel_order)\(' kite-momentum-rebalancer/app --include='*.py' | grep -v 'app/kite_client.py' | wc -l | tr -d ' '
  EXPECT: 0
  EVIDENCE: 0

- [x] F10: the whole desk suite is green after the change
  CHECK: cd kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q 2>&1 | tail -1
  EXPECT: /^\d+ passed(, \d+ skipped)?/
  EVIDENCE: 2436 passed, 147 skipped, 203 warnings, 12 subtests passed in 86.77s (0:01:26)
