# Gates: SC2 independent adversarial audit

Scope: Independently falsify SC2's accuracy, tenant/order safety, bounded-query/idempotence
claims, and large-input resource behavior without overlapping implementation ownership.

- [x] G1: Exact money, chain-link, window, volatility, and rounding math survives independent boundary tests
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_metrics.py -q -rA
  EXPECT: /PASSED /
  EVIDENCE: 2026-08-23 — the independent full core run passed 31/31 tests. A separate seeded
  50,000-case Fraction oracle agreed exactly with both minimum-investment ceil ratios and every
  whole-share floor, including values beyond the ambient Decimal context.

- [x] G2: Catalog/watchlist rejects anonymous and non-sole identities and exposes no user-selectable tenant input
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc_audit_test uv run pytest services/api/tests/test_curated_catalog.py -q -rA -k 'auth or sole or tenant or user_id'
  EXPECT: /PASSED /
  EVIDENCE: 2026-08-23 — the independent private-database API run passed all 30 tests, including
  anonymous rejection on every route, 404 concealment from a second authenticated account, and
  proof that query/body `user_id` cannot choose the watch-row owner.

- [x] G3: SC2 mounts no execution/order/payment/network path and catalog queries stay bounded as row count grows
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc_audit_test uv run pytest services/api/tests/test_curated_catalog.py -q -rA -k 'query_count or large_catalog or forbidden or no_order'
  EXPECT: /PASSED /
  EVIDENCE: 2026-08-23 — the same 30/30 run passed AST import and OpenAPI route exclusions;
  80 added catalog rows stayed at two statements including auth, while an 80-item watchlist with
  exact daily change stayed at three statements including auth and its ranked prior-NAV read.

- [x] G4: Metrics recomputation and SCAN projection are exact, atomic, bounded, and true row/content no-ops on rerun
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc_audit_test uv run pytest services/worker/tests/test_curated_metrics_job.py services/worker/tests/test_curated_scan_projection.py -q -rA -k 'writes_exact or idempotent or coverage or bounded_interval or readded or query_count or causal or projection'
  EXPECT: /PASSED /
  EVIDENCE: 2026-08-23 — the independent post-diff audit passed 17/17 selected worker tests:
  adjusted-vs-raw exactness, content/timestamp no-op, atomic missing coverage, late inception,
  removed-then-readded fallback, fixed-50 15-year interval sizing, pre-SQL churn refusal, flat
  query count, causal post-publish dispatch, deterministic projection, malformed provenance,
  missing instruments, same-identity conflict, forbidden-row absence, and the 50-name cap.

- [x] G5: Large-history computation stays within the specified time/memory envelope and adds no quadratic or ambient-I/O path
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_metrics.py -q -rA -k performance
  EXPECT: /PASSED /
  EVIDENCE: 2026-08-23 — the prescribed 8,000-day core performance/memory test passed again.
  An independent 4,000-date x 50-name worker-shaped probe computed 200,000 price rows in 0.608 s
  (about 152 MB process max RSS); that measurement exposed and drove the interval-bounded loader
  now covered by the worker leaf's fixed-50 and churn-limit tests.

- [x] G6: Expert reread plus an independent defect hunt leaves no unresolved high-value SC2 finding
  EVIDENCE: 2026-08-23 — six independent findings were resolved in their owning leaves: calendar
  age windows, deterministic include-new, watchlist daily-change/query bounds, the incorrect
  worker golden oracle, causal late-publish dispatch, and union-history memory amplification.
  Final source reread found interval loads capped at 10,000 bind rows/250,000 prices, all desired
  metrics computed before mutation, exact prior-NAV math, and no execution/network/payment import.
  Targeted ruff, diff-check, task binding, forbidden-import scan, and every audit command pass.
