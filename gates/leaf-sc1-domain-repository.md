# Gates: SC1 pure domain and repository

Scope: Validate immutable exact-weight versions purely, then persist them through one service seam.

- [x] G1: Exact-weight, duplicate, range, and label domain properties pass
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_baskets.py -q -rA
  EXPECT: /PASSED /
  EVIDENCE: 2026-08-23 — exact CHECK exited 0 with 20 PASSED cases, including exact total, storage precision, malformed runtime values, duplicates, ranges, and freezing.

- [x] G2: The pure domain module imports no database, network, disk, or clock dependencies
  CHECK: cd decile-blueprint && ! rg -n 'sqlalchemy|httpx|requests|Path\(|open\(|datetime\.now|date\.today|socket' packages/core/src/baskfy_core/curated_baskets.py && echo pure-boundary-clean
  EXPECT: /pure-boundary-clean/
  EVIDENCE: 2026-08-23 — exact CHECK exited 0 and printed `pure-boundary-clean`; forbidden-import scan returned no matches.

- [x] G3: Repository rejects bad weights before writing and persists a valid immutable version
  CHECK: cd decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc_domain_test uv run pytest services/api/tests/test_curated_basket_repository.py -q -rA
  EXPECT: /PASSED /
  EVIDENCE: 2026-08-23 — exact CHECK exited 0 with 12 PASSED cases against migrated `baskfy_sc_domain_test`; zero-write refusals, persistence, date order, and the append-only trigger all passed.

- [x] G4: Version labels and added/removed counts are derived from the prior version, not trusted from callers
  CHECK: cd decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc_domain_test uv run pytest services/api/tests/test_curated_basket_repository.py -q -rA -k 'diff or label or counts'
  EXPECT: /PASSED /
  EVIDENCE: 2026-08-23 — exact filtered CHECK exited 0 with 4 PASSED cases covering GENESIS, NO_CHANGE, CHANGED, add/remove counts, segment changes, and weight changes.

- [x] G5: Large constituent validation stays linear and bounded
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_baskets.py -q -rA -k large
  EXPECT: /PASSED /
  EVIDENCE: 2026-08-23 — exact CHECK exited 0; the 10,000-name one-pass/set validation stayed below its 2 s and 4 MiB peak-allocation bounds.

- [x] G6: Expert reread, defect hunt, and free polish find no remaining domain/repository defect
  EVIDENCE: 2026-08-23 — four-pass sweep completed; targeted Ruff, format, strict mypy, 8 no-escape-hatch tests, and TODO/Any/type-ignore scan all exited 0. Fixed 5 defects found during reread: 4 dp write-rounding drift, malformed runtime fields escaping typed errors, bool basket identity aliasing id 1, required manager fixture fields, and trigger-message mismatch.
