# Gates: SC3 product-plan and desk-journal lifecycle

Scope: Create but never execute plans, then reconcile only confirmed journal truth into SC state.

- [ ] G1: All five authenticated plan operations persist DRAFT→PLANNED batches and desk-compatible intent without an execution path
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc3_plan_test uv run pytest services/api/tests/test_curated_plan_lifecycle.py -q -rA -k 'invest or apply or exit'
  EXPECT: /PASSED /
  EVIDENCE: pending

- [ ] G2: Every plan endpoint rejects closed NSE sessions before either journal receives a row and returns the next-open payload
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc3_plan_test uv run pytest services/api/tests/test_curated_plan_lifecycle.py -q -rA -k 'closed or boundary or holiday'
  EXPECT: /PASSED /
  EVIDENCE: pending

- [ ] G3: Confirmed/partial/failed journal fills reconcile idempotently; holdings never move optimistically or beyond confirmed quantity
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc3_plan_test uv run pytest services/api/tests/test_curated_plan_lifecycle.py -q -rA -k 'fill or reconcile or holding'
  EXPECT: /PASSED /
  EVIDENCE: pending

- [ ] G4: A plan expires strictly after 30 minutes and apply expiry restores PENDING without changing holdings
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc3_plan_test uv run pytest services/api/tests/test_curated_plan_lifecycle.py -q -rA -k expire
  EXPECT: /PASSED /
  EVIDENCE: pending

- [ ] G5: Full DRY_RUN fixture loop reaches invest EXECUTED, v2 diff, apply EXECUTED/APPLIED with hand-computed exact quantities
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc3_plan_test uv run pytest services/api/tests/test_curated_plan_lifecycle.py -q -rA -k end_to_end
  EXPECT: /PASSED /
  EVIDENCE: pending

- [ ] G6: AST/OpenAPI prove no order-submit route/import; expert security/performance/concurrency reread and free polish find no defect
  EVIDENCE: pending
