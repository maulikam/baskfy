# Gates: SC3 immutable version publishing and timeline

Scope: Publish manual/from-scan versions atomically and expose their exact timeline and user side effects.

- [ ] G1: Manual publish validates through the SC1 seam and appends an immutable version/constituents atomically
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc3_publish_test uv run pytest services/api/tests/test_curated_publishing.py -q -rA -k manual
  EXPECT: /PASSED /
  EVIDENCE: pending

- [ ] G2: From-scan publishing binds immutable scan provenance and refuses missing, stale, foreign, or replay-conflicting runs
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc3_publish_test uv run pytest services/api/tests/test_curated_publishing.py -q -rA -k scan
  EXPECT: /PASSED /
  EVIDENCE: pending

- [ ] G3: Publish creates exactly one ENGINE post/action per affected sole user and PENDING state per ACTIVE investment
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc3_publish_test uv run pytest services/api/tests/test_curated_publishing.py -q -rA -k 'side_effect or pending or idempotent'
  EXPECT: /PASSED /
  EVIDENCE: pending

- [ ] G4: The authenticated timeline is chronological, latest-version correct, bounded, and sole-user/tenant safe
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc3_publish_test uv run pytest services/api/tests/test_curated_publishing.py -q -rA -k timeline
  EXPECT: /PASSED /
  EVIDENCE: pending

- [ ] G5: Publishing has no execution/payment/network imports and rollback leaves no partial version, post, action, or state
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc3_publish_test uv run pytest services/api/tests/test_curated_publishing.py -q -rA -k 'rollback or forbidden'
  EXPECT: /PASSED /
  EVIDENCE: pending

- [ ] G6: Expert reread, concurrency/idempotence/security/query-budget pass, defect hunt, and free polish find no remaining defect
  EVIDENCE: pending
