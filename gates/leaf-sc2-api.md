# Gates: SC2 authenticated catalog and watchlist API

Scope: Serve the catalog/detail/manager/collection surfaces and sole-user watchlist without N+1 queries.

- [x] G1: Every documented catalog filter and sort query parameter round-trips
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc_api_test uv run pytest services/api/tests/test_curated_catalog.py -q -rA -k 'filter or sort or query'
  EXPECT: /PASSED /
  EVIDENCE: Fresh selector run passed 11 tests: repeated normalized categories, all seven other
  params, four deterministic sorts, exact calendar headline windows, include-new boundary, and
  both bounded-query tests.

- [x] G2: Detail, manager, and collection responses use latest versions/metrics and honest missing states
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc_api_test uv run pytest services/api/tests/test_curated_catalog.py -q -rA -k 'detail or manager or collection or missing'
  EXPECT: /PASSED /
  EVIDENCE: Fresh selector run passed 8 tests, including exclusion of a scheduled future version,
  latest-metric constituents, collection order, registration-pending data, and RFC 9457 404/503.

- [x] G3: Watchlist CRUD records the stored NAV baseline, is idempotent, and computes moved-since-watch
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc_api_test uv run pytest services/api/tests/test_curated_catalog.py -q -rA -k watchlist
  EXPECT: /PASSED /
  EVIDENCE: Full private-DB run passed CRUD, conflict-no-op timestamp/baseline preservation,
  delete+rewatch reset, exact moved percentage, exact consecutive-session daily change, and null
  daily change for a missing/gapped prior snapshot; a second-exchange regression proves the
  continuity check reads only the NSE calendar.

- [x] G4: Anonymous/non-sole users cannot read or mutate SC surfaces and request bodies cannot select user identity
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc_api_test uv run pytest services/api/tests/test_curated_catalog.py -q -rA -k 'auth or sole or tenant or user_id'
  EXPECT: /PASSED /
  EVIDENCE: Full run passed 7 anonymous route cases, non-sole 404 read/write cases, rejected
  catalog user_id query injection, and ignored PUT body identity while persisting the sole user.

- [x] G5: Catalog query count stays bounded as baskets grow and no route imports execution/payment/network clients
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc_api_test uv run pytest services/api/tests/test_curated_catalog.py -q -rA -k 'query_count or large_catalog or forbidden'
  EXPECT: /PASSED /
  EVIDENCE: 83-card catalog stayed at auth+1 statement; 80-item watchlist with daily change stayed
  at auth+2 statements. A forced two-row cap reports total=3 and truncated=true. AST import scan
  and OpenAPI route scan passed; CORS permits metadata PUT.

- [x] G6: Expert reread, security/performance pass, defect hunt, and free polish find no remaining API defect
  EVIDENCE: Four passes fixed future-version leakage, 365/30-day window drift, stale CURRENT_DATE
  filtering, transaction-stable rewatch timestamps, gapped “daily” returns, unbounded slug/category
  input, and full-catalog watch writes. Final result: 30/30 DB tests plus ruff/format/mypy clean.
