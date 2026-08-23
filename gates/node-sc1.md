# Gates: SC1 integration

Scope: Integrate schema, pure validation/repository, and seeds into one green SC1 module.

- [x] G1: All three SC1 leaf ledgers are fully met with evidence
  CHECK: node /Users/maulikdave/.agents/skills/unlazy/scripts/gate-check.mjs --status gates/leaf-sc1-schema.md && node /Users/maulikdave/.agents/skills/unlazy/scripts/gate-check.mjs --status gates/leaf-sc1-domain-repository.md && node /Users/maulikdave/.agents/skills/unlazy/scripts/gate-check.mjs --status gates/leaf-sc1-seeding.md
  EXPECT: /UNMET: 0/
  EVIDENCE: 2026-08-23 — parent `--status` checks report ALL MET for schema 6/6, domain/repository 6/6, and seeding 6/6.

- [x] G2: Migrate, seed, and migrate again succeeds against the SC database
  CHECK: cd decile-blueprint && BASKFY_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc_test make migrate && BASKFY_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc_test uv run python -m baskfy_api.seed reference && BASKFY_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc_test make migrate
  EXPECT: cb_manager: 2 rows
  EVIDENCE: 2026-08-23 — exited 0 on `baskfy_sc_test`; seed reported app_user 1, cb_manager 2, exchange 1, index_def 14, plan 3, screen 6; second migration was a no-op.

- [x] G3: SC1 core and API tests pass together
  CHECK: cd decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc_test uv run pytest packages/core/tests/test_curated_baskets.py services/api/tests/test_curated_basket_schema.py services/api/tests/test_curated_basket_repository.py services/api/tests/test_curated_basket_seed.py -q
  EXPECT: /[0-9]+ passed/
  EVIDENCE: 2026-08-23 — parent integration run exited 0 with 46 passed across pure domain, migration/schema, live repository, and seed suites.

- [x] G4: Lint and strict typing pass across the touched tree
  CHECK: cd decile-blueprint && DRY_RUN=true make lint
  EXPECT: apps/web lint: Done
  EVIDENCE: 2026-08-23 — parent found and formatted three files missed by leaf verification; rerun passed Ruff check/format, strict mypy (300 files), API-client TypeScript, and web TypeScript/ESLint (one known TanStack warning, zero errors).

- [x] G5: Desk suite remains green under simulation
  CHECK: cd kite-momentum-rebalancer && DRY_RUN=true /Users/maulikdave/Documents/projects/baskfy/kite-momentum-rebalancer/.venv/bin/python -m pytest -q
  EXPECT: /[0-9]+ passed/
  EVIDENCE: 2026-08-23 — unchanged source desk run in its authoritative local-fixture worktree with DRY_RUN=true: 1,328 passed, 17 skipped, 12 subtests passed. The isolated worktree cannot collect one test because its ignored sample CSV is intentionally absent.

- [x] G6: STATUS and DECISIONS identify what SC1 did and what remains undone
  CHECK: rg -n 'SC1.*✅|SC1\.[0-9].*UNREVIEWED|SC2–SC12 are not done' docs/smallcase/STATUS.md docs/smallcase/DECISIONS-SC.md
  EXPECT: SC1
  EVIDENCE: 2026-08-23 — STATUS marks SC1 green, names all 18 tables and verification results, and states SC2–SC12 plus Track-B gating are not done; DECISIONS records SC1.1–SC1.3 as ⚠ UNREVIEWED with rejected alternatives and reversal paths.
