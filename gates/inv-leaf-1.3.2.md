# Gates: 1.3.2 Drift-repair UI

Scope: scan + fix endpoints rebase the intended ledger to broker qty; UI Fix now; no orders.

- [x] G1: Drift API tests green
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest services/api/tests/test_curated_drift_api.py --tb=line 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: ....                                                                     [100%] | 4 passed in 0.72s

- [x] G2: drift-repair testid exists
  CHECK: rg -n "drift-repair" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/investments/drift-repair.tsx
  EXPECT: drift-repair
  EVIDENCE: 70:      data-testid="drift-repair"

- [x] G3: Router has no OrderGateway
  CHECK: rg -n "OrderGateway|place_order|/execute" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/services/api/src/baskfy_api/routers/curated_drift.py || echo NONE
  EXPECT: NONE
  EVIDENCE: NONE
