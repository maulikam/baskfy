# Gates: leaf-1.6.3-engage

Scope: Updates feed + pending actions

- [x] G1: pending action model used
  CHECK: rg -n 'CbPendingAction|pending_action' decile-blueprint/services/api/src/baskfy_api -g '*.py' | head -1
  EXPECT: Pending|pending
  EVIDENCE: `decile-blueprint/services/api/src/baskfy_api/routers/curated_engage.py` imports `CbPendingAction` (list/dismiss/resolve); also `curated_versions.py` publish side-effects. Routes: `GET /cb/pending-actions`, `GET /cb/updates`, dismiss/resolve. Tests: `tests/test_curated_engage.py`.


<!-- integrity: security, performance, memory, accuracy required -->
