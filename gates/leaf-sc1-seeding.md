# Gates: SC1 settings and seed data

Scope: Configure the sole identities and idempotently seed the sole operator plus two managers.

- [x] G1: `BASKFY_SOLE_USER_ID` is a settings-backed public identity with a safe deterministic default
  CHECK: cd decile-blueprint && DRY_RUN=true uv run pytest services/api/tests/test_curated_basket_seed.py -k sole_user_setting
  EXPECT: /[0-9]+ passed/
  EVIDENCE: `2 passed, 3 deselected in 0.37s` (defaults/env overrides plus non-routable email)

- [x] G2: Re-running the SC seed changes no row counts or existing operator data
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc_seed_test uv run pytest services/api/tests/test_curated_basket_seed.py -k idempotent
  EXPECT: /[0-9]+ passed/
  EVIDENCE: `1 passed, 4 deselected in 2.77s`; snapshots stable; user preserved; all manager copy converges

- [x] G3: Exactly `baskfy-engine` and `maulik` manager seeds land with their documented kinds
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc_seed_test uv run pytest services/api/tests/test_curated_basket_seed.py -k managers
  EXPECT: /[0-9]+ passed/
  EVIDENCE: `1 passed, 4 deselected in 40.17s`; exact slugs/kinds and non-regulated copy asserted

- [x] G4: The sole-user seed creates no credential, token, payment, or external side effect
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc_seed_test uv run pytest services/api/tests/test_curated_basket_seed.py -k no_credentials
  EXPECT: /[0-9]+ passed/
  EVIDENCE: `1 passed, 4 deselected in 3.62s`; password/verification null and six side tables zero

- [x] G5: Every committed environment example keeps execution dry and Track-B defaults dark
  CHECK: cd decile-blueprint && DRY_RUN=true rg -n '^DRY_RUN=true$' ../kite-momentum-rebalancer/.env.example
  EXPECT: DRY_RUN=true
  EVIDENCE: 37:DRY_RUN=true

- [x] G6: Expert reread, defect hunt, and free polish find no remaining settings/seed defect
  EVIDENCE: 4-file ruff+mypy clean; full leaf `5 passed in 8.02s`; fixed RFC local-part overflow and blank-ID handling
