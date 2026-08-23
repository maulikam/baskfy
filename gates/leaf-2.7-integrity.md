# Gates: leaf-2.7-integrity

Scope: New AC surfaces: no execute; create/sip source clean; accuracy remeasure

- [x] G1: create+sip routers/tasks have no OrderGateway
  CHECK: rg -n 'OrderGateway|place_order|confirm=true' decile-blueprint/services/api/src/baskfy_api/routers/curated_create.py decile-blueprint/services/worker/src/baskfy_worker/tasks/curated_sip.py 2>/dev/null; echo exit:$?
  EXPECT: exit:1
  EVIDENCE: parent 2026-08-23 — rg_exit:1 (no matches); test_ac_no_orders.py .... [100%]

- [x] G2: fee accuracy still green
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_accounting.py -q --tb=no -k fee 2>&1 | tail -1
  EXPECT: [100%]
  EVIDENCE: parent — ........... [100%] (11 fee)

<!-- integrity: security, performance, memory, accuracy required -->
