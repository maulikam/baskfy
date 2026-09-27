# Gates: LV3 — account-wide risk and capital, shared across processes

Scope: one durable risk ledger that the desk, swing-monitor, twt-auto and the supervisor all read and write under a lock; reservations released on rejection; holdings counted; a kill switch that reaches every process.

- [x] K1: RiskManager accepts a RiskStateStore; with one, pre_order reloads under lock before deciding and saves after; the JSON file path still works unchanged
  CHECK: cd decile-blueprint && uv run pytest packages/execution/tests/test_risk_store.py 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 9 passed in 0.07s

- [x] K2: two RiskManagers over one store cannot spend the same cap twice — the second pre_order sees the first's reservation (simulated Swing + TWT entries)
  CHECK: cd decile-blueprint && uv run pytest packages/execution/tests/test_risk_store.py -k 'two_processes or shared_cap' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 1 passed, 8 deselected in 0.06s

- [x] K3: release() gives an unfilled reservation back; seed_positions() counts an existing (manual) holding toward the per-symbol and gross exposure; kill() in one manager is seen by the other
  CHECK: cd decile-blueprint && uv run pytest packages/execution/tests/test_risk_store.py -k 'release or seed or kill' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 3 passed, 6 deselected in 0.06s

- [x] K4: the desk's PgRiskStateStore is a real Postgres row lock (SELECT … FOR UPDATE on desk.risk_ledger) and app.main.gateway() wires it when DB_BACKEND is postgres
  CHECK: cd kite-momentum-rebalancer && grep -c 'FOR UPDATE' app/core/risk_store.py && grep -c 'PgRiskStateStore' app/main.py
  EXPECT: /[1-9]/
  EVIDENCE: 3 | 3

- [x] K5: the reconciler releases the unfilled remainder of a dead or partially filled order (LV2 ↔ LV3 wiring)
  CHECK: cd kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q tests/test_reconcile.py -k 'release' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 2 passed, 19 deselected in 0.78s

- [x] K6: execution package tests and mypy strict stay green
  CHECK: cd decile-blueprint && uv run pytest packages/execution 2>&1 | tail -1 && uv run mypy packages/execution/src 2>&1 | tail -1
  EXPECT: /Success: no issues/
  EVIDENCE: ............................................................             [100%] | Success: no issues found in 11 source files
