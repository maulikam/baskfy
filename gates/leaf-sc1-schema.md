# Gates: SC1 schema and migration

Scope: Define every documented `cb_` storage model and an upgrade/downgrade-safe migration.

- [x] G1: All 18 documented `cb_` tables exist in SQLAlchemy metadata
  CHECK: cd decile-blueprint && uv run python -c "from baskfy_core.models import Base; names=sorted(n for n in Base.metadata.tables if n.startswith('cb_')); print(f'{len(names)} cb_ tables'); print(' '.join(names))"
  EXPECT: 18 cb_ tables
  EVIDENCE: `18 cb_ tables`; the printed set exactly matches docs/smallcase/03 (2026-08-23).

- [x] G2: Schema-vs-spec tests pass without undocumented tables or float columns
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_schema_matches_docs.py
  EXPECT: /[0-9]+ passed/
  EVIDENCE: `169 passed in 0.42s` (2026-08-23).

- [x] G3: SC1 migration creates the real database schema, constraints, indexes, and immutability triggers
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc_schema_test uv run pytest services/api/tests/test_curated_basket_schema.py
  EXPECT: /[0-9]+ passed/
  EVIDENCE: `9 passed in 13.47s` against disposable `baskfy_sc_schema_test` (2026-08-23).

- [x] G4: Upgrade twice is a no-op and downgrade/upgrade round-trips
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc_schema_test uv run pytest services/api/tests/test_curated_basket_schema.py -k 'idempotent or round_trip'
  EXPECT: /[0-9]+ passed/
  EVIDENCE: `2 passed, 7 deselected in 5.39s` (2026-08-23).

- [x] G5: Foreign-key access paths are indexed and exact numeric columns contain no float type
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc_schema_test uv run pytest services/api/tests/test_curated_basket_schema.py -k 'indexes or numeric'
  EXPECT: /[0-9]+ passed/
  EVIDENCE: `2 passed, 7 deselected in 2.85s` (2026-08-23).

- [x] G6: Expert reread, defect hunt, and free polish find no remaining schema defect
  EVIDENCE: Four leaf passes completed; targeted Ruff clean, mypy `Success: no issues found in 3 source files`, `git diff --check` clean, no TODO/FIXME/Float/I/O/order call sites, and `alembic check` reported no `cb_` drift (only pre-existing non-SC drift). Parent integration then caught and mechanically formatted three files missed by the leaf's format check; full repository format/lint/type gates pass on 2026-08-23.
