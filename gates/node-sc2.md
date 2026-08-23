# Gates: SC2 integration

Scope: Integrate exact math, authenticated catalog/watchlist, and idempotent EOD metrics/SCAN projection.

- [x] G1: All SC2 implementation and independent-audit leaf ledgers are fully met with evidence
  CHECK: node /Users/maulikdave/.agents/skills/unlazy/scripts/gate-check.mjs --status gates/leaf-sc2-math.md && node /Users/maulikdave/.agents/skills/unlazy/scripts/gate-check.mjs --status gates/leaf-sc2-api.md && node /Users/maulikdave/.agents/skills/unlazy/scripts/gate-check.mjs --status gates/leaf-sc2-job-seed.md && node /Users/maulikdave/.agents/skills/unlazy/scripts/gate-check.mjs --status gates/leaf-sc2-audit.md
  EXPECT: /UNMET: 0/
  EVIDENCE: 2026-08-23 — math, API, job/seed, and independent adversarial audit ledgers each
  report ALL MET (6/6), for 24 evidenced leaf gates.

- [x] G2: SC2 suites pass together on the disposable integration database
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc_test uv run pytest packages/core/tests/test_curated_metrics.py services/api/tests/test_curated_catalog.py services/worker/tests/test_curated_metrics_job.py services/worker/tests/test_curated_scan_projection.py -q -rA
  EXPECT: /PASSED /
  EVIDENCE: 2026-08-23 — 85 tests passed together: 31 pure math, 32 authenticated API,
  13 metrics-job/orchestration, and 9 SCAN-projection tests. The parent added and reran the ninth
  projection proof that a different scan id/data version cannot silently relabel immutable
  genesis even when its symbols are identical.

- [x] G3: Migrate, reference/fixture seed, curated projection, metrics rerun, and migrate remain idempotent
  EVIDENCE: 2026-08-23 — integration DB was already at `0014_curated_baskets`; reference seed
  ran twice with identical counts and a final upgrade stayed at head. Projection tests prove both
  content/timestamp no-op and provenance-conflict refusal; metrics tests prove exact-content and
  `computed_at` no-op on the second exact-date run.

- [x] G4: Full repository lint/strict typing and generated API client contract pass
  CHECK: cd decile-blueprint && DRY_RUN=true make lint
  EXPECT: apps/web lint: Done
  EVIDENCE: 2026-08-23 — generated OpenAPI and TypeScript schema include all six path groups;
  repository Ruff, 375-file format check, strict mypy over 308 source files, API-client TypeScript,
  and web TypeScript/ESLint pass with zero errors (one pre-existing TanStack compiler warning).
  API-client contract/parity tests remain 110/110 green and OpenAPI `--check` passes.

- [x] G5: Desk suite stays green under DRY_RUN and no SC API route can reach execution
  EVIDENCE: 2026-08-23 — the authoritative desk worktree passes 1,328 tests, 17 skips, and 12
  subtests in 46.11 s with `DRY_RUN=true`. SC route AST/OpenAPI tests exclude execution,
  payment, provider/network imports and expose only catalog reads plus watch-metadata PUT/DELETE.

- [x] G6: STATUS/DECISIONS are lossless and explicit that SC3–SC12 remain undone
  CHECK: rg -n 'SC2.*✅|SC2\.[0-9].*UNREVIEWED|SC3–SC12 are not done' docs/smallcase/STATUS.md docs/smallcase/DECISIONS-SC.md
  EXPECT: SC2
  EVIDENCE: 2026-08-23 — STATUS records exact SC2 surfaces, limits, evidence, safety posture,
  and resume boundary; SC2.1–SC2.5 record every judgment with rejected alternatives and reversal
  paths. SC3–SC12 are explicitly not done.
