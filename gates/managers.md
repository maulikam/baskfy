# Gates: Third-party managers — a manager becomes a person who can join (tree 3, solo)

Scope: `cb_manager` is a seed row today. This makes it an identity a real person can hold, apply
for, be reviewed against, and publish under. Revenue share is built as SCHEMA ONLY behind a dark
flag — the amounts are D7 and stay Maulik's.

Verified before writing these gates, not assumed:
- Exactly two seed managers exist: `MANAGER_SEED_ROWS` at `services/api/src/baskfy_api/curated_seed.py:38` — "Baskfy Engine" (ENGINE) and "Maulik" (HUMAN).
- `cb_manager` (models/curated_baskets.py:115) has NO `user_id`, no application, no review state. It is a row, not a person.
- `sebi_reg_no` is a bare nullable String — no type, no format check, no validity window, no verification state.
- `cb_basket.visibility` is PUBLISHED/PRIVATE but nothing gates WHO may flip it, and there is no manager-facing route.
- No revenue-share entity exists anywhere.
- Alembic head is `0019_portfolio_graph`; this work adds `0020_manager_identity`.

Governance, from the root CLAUDE.md and NOT negotiable here:
- Track B flags (`subscriptions_enabled`, `fee_collection_enabled`, `public_signup_enabled`) stay FALSE.
- D7 pricing amounts are human-track. No default revenue-share rate is invented anywhere.
- D3 posture B is UNREVIEWED; nothing here asserts that a registration makes anyone compliant.
- No order path. No execute route. `packages/core` stays I/O-free.

## Leaf 1 — a manager is a person

- [x] G1: `cb_manager` carries a nullable `user_id` FK to `app_user` — nullable because the two seed managers are not people
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL="$(grep -E '^BASKFY_TEST_DATABASE_URL=' .env | cut -d= -f2- | tr -d '"')" && uv run python -c "from baskfy_core.models import Base; c=Base.metadata.tables['cb_manager'].c; ok='user_id' in c and c.user_id.nullable and any('app_user.id' in str(f.target_fullname) for f in c.user_id.foreign_keys); print('MANAGER_USER_OK' if ok else 'MANAGER_USER_MISSING')"
  EXPECT: MANAGER_USER_OK
  EVIDENCE: MANAGER_USER_OK

- [x] G2: one person cannot hold two manager identities (unique on user_id where present)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL="$(grep -E '^BASKFY_TEST_DATABASE_URL=' .env | cut -d= -f2- | tr -d '"')" && uv run python -c "from baskfy_core.models import Base; t=Base.metadata.tables['cb_manager']; idx=[i for i in t.indexes if 'user_id' in [c.name for c in i.columns] and i.unique]; uq=[c for c in t.constraints if c.__class__.__name__=='UniqueConstraint' and 'user_id' in [x.name for x in c.columns]]; print('MANAGER_UNIQUE_OK' if idx or uq else 'MANAGER_UNIQUE_MISSING')"
  EXPECT: MANAGER_UNIQUE_OK
  EVIDENCE: MANAGER_UNIQUE_OK

- [x] G3: an onboarding state machine exists with a documented, constrained set of states
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL="$(grep -E '^BASKFY_TEST_DATABASE_URL=' .env | cut -d= -f2- | tr -d '"')" && uv run python -c "from baskfy_core.models import Base; t=Base.metadata.tables['cb_manager']; s=' '.join(str(x.sqltext) for x in t.constraints if x.__class__.__name__=='CheckConstraint'); need=('DRAFT','SUBMITTED','APPROVED','REJECTED','SUSPENDED'); print('STATES_OK' if all(n in s for n in need) else 'STATES_MISSING '+str([n for n in need if n not in s]))"
  EXPECT: STATES_OK
  EVIDENCE: STATES_OK

- [x] G4: the state machine's legal transitions are pure and asserted — no path re-approves a suspended manager without review
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests/test_manager_onboarding.py -k "transition or suspend" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ....................                                                     [100%] | 20 passed, 9 deselected in 0.08s

- [x] G5: the two seeded managers still load and still have no user (seeding is unchanged and idempotent)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL="$(grep -E '^BASKFY_TEST_DATABASE_URL=' .env | cut -d= -f2- | tr -d '"')" && uv run pytest services/api/tests -k "curated_seed or seed_idempotent" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed|no tests ran/m
  EVIDENCE: .                                                                        [100%] | 1 passed, 1211 deselected in 4.03s

## Leaf 2 — SEBI registration, captured honestly

- [x] G6: registration is structured — a constrained type, a number, a validity window, and a verification state
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL="$(grep -E '^BASKFY_TEST_DATABASE_URL=' .env | cut -d= -f2- | tr -d '"')" && uv run python -c "from baskfy_core.models import Base; c=Base.metadata.tables['cb_manager'].c; need=('sebi_reg_type','sebi_reg_valid_from','sebi_reg_valid_to','sebi_reg_verified_at'); miss=[n for n in need if n not in c]; print('REG_FIELDS_OK' if not miss else 'MISSING '+str(miss))"
  EXPECT: REG_FIELDS_OK
  EVIDENCE: REG_FIELDS_OK

- [x] G7: the registration-number format check is a PURE function in packages/core, and it rejects malformed input rather than coercing it
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests/test_sebi_registration.py --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: .....................................                                    [100%] | 37 passed in 0.06s

- [x] G8: Law 1 holds for the new pure module — no DB, no network, no clock, no disk
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && echo "HITS=$(grep -cE 'sqlalchemy|httpx|requests|datetime\.now|utcnow|(^|[^A-Za-z0-9_])open\(|Session' packages/core/src/baskfy_core/sebi_registration.py 2>/dev/null || echo 0)"
  EXPECT: HITS=0
  EVIDENCE: HITS=0 | 0

- [x] G9: a captured registration defaults to UNVERIFIED and nothing in the codebase claims a registration means compliance (D3 is UNREVIEWED)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests/test_sebi_registration.py -k "unverified or not_compliance or disclaimer" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ..                                                                       [100%] | 2 passed, 35 deselected in 0.06s

## Leaf 3 — publishing

- [x] G10: only an APPROVED manager may publish, and only their own basket
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL="$(grep -E '^BASKFY_TEST_DATABASE_URL=' .env | cut -d= -f2- | tr -d '"')" && uv run pytest services/api/tests/test_manager_publishing.py -k "approved or own" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ....                                                                     [100%] | 4 passed, 17 deselected in 3.05s

- [x] G11: publishing another manager's basket is refused as NOT_FOUND — existence is never leaked
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL="$(grep -E '^BASKFY_TEST_DATABASE_URL=' .env | cut -d= -f2- | tr -d '"')" && uv run pytest services/api/tests/test_manager_publishing.py -k "other or not_found or tenant" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ...                                                                      [100%] | 3 passed, 18 deselected in 3.09s

- [x] G12: no order path was added — publishing records a listing, it never places anything
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && echo "ORDER_HITS=$(grep -rnE 'place_order|OrderGateway|/execute|confirm=true' services/api/src/baskfy_api/routers/managers.py 2>/dev/null | wc -l | tr -d ' ')"
  EXPECT: ORDER_HITS=0
  EVIDENCE: ORDER_HITS=0

- [x] G13: the documented-surface test still passes — every new route is registered in EXPECTED_PATHS
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL="$(grep -E '^BASKFY_TEST_DATABASE_URL=' .env | cut -d= -f2- | tr -d '"')" && uv run pytest services/api/tests/test_api_artifacts.py --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ............                                                             [100%] | 12 passed in 1.81s

## Leaf 4 — revenue share, dark and amount-free

- [x] G14: a revenue-share agreement table exists with NO default rate anywhere — the amount is D7 and is supplied, never invented
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL="$(grep -E '^BASKFY_TEST_DATABASE_URL=' .env | cut -d= -f2- | tr -d '"')" && uv run python -c "from baskfy_core.models import Base; t=Base.metadata.tables['cb_manager_revenue_share']; c=t.c['rate_bps']; import decimal; d=c.server_default; print('NO_DEFAULT_RATE_OK' if d is None else 'INVENTED_DEFAULT '+str(d.arg))"
  EXPECT: NO_DEFAULT_RATE_OK
  EVIDENCE: NO_DEFAULT_RATE_OK

- [x] G15: the revenue-share surface is dark — it 404s while `fee_collection_enabled` is false, following the existing track_b pattern
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL="$(grep -E '^BASKFY_TEST_DATABASE_URL=' .env | cut -d= -f2- | tr -d '"')" && uv run pytest services/api/tests -k "revenue_share and dark" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: .                                                                        [100%] | 1 passed, 1211 deselected in 3.48s

- [x] G16: all three Track B flags still default false
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL="$(grep -E '^BASKFY_TEST_DATABASE_URL=' .env | cut -d= -f2- | tr -d '"')" && uv run pytest services/api/tests/test_track_b_gates.py --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: .......                                                                  [100%] | 7 passed in 0.47s

## Integration

- [x] G17: migration 0020 exists, chains off 0019, is the single head, and round-trips
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_DATABASE_URL="$(grep -E '^BASKFY_DATABASE_URL=' .env | cut -d= -f2- | tr -d '"')" && cd services/api && uv run alembic upgrade head >/dev/null 2>&1 && uv run alembic downgrade 0019_portfolio_graph >/dev/null 2>&1 && uv run alembic upgrade head >/dev/null 2>&1 && uv run alembic heads 2>&1 | tail -1
  EXPECT: 0020_manager_identity
  EVIDENCE: 0020_manager_identity (head)

- [x] G18: the full core suite and the touched API suites pass serially
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL="$(grep -E '^BASKFY_TEST_DATABASE_URL=' .env | cut -d= -f2- | tr -d '"')" && uv run pytest packages/core services/api/tests/test_manager_publishing.py services/api/tests/test_api_artifacts.py services/api/tests/test_track_b_gates.py --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ............                                                             [100%] | 1666 passed, 2 skipped in 75.03s (0:01:15)

- [x] G19: every file this work owns is ruff-, format- and mypy-clean, and no escape hatches were added
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run ruff check packages/core/src/baskfy_core/sebi_registration.py packages/core/src/baskfy_core/manager_onboarding.py packages/core/src/baskfy_core/models/curated_baskets.py services/api/src/baskfy_api/routers/managers.py >/dev/null 2>&1 && uv run ruff format --check packages/core/src/baskfy_core/sebi_registration.py packages/core/src/baskfy_core/manager_onboarding.py services/api/src/baskfy_api/routers/managers.py >/dev/null 2>&1 && uv run mypy packages/core/src services/api/src >/dev/null 2>&1 && uv run pytest packages/core/tests/test_no_escape_hatches.py >/dev/null 2>&1 && echo GATE_OK || echo GATE_FAILED
  EXPECT: GATE_OK
  EVIDENCE: GATE_OK

- [x] G20: the judgement calls are recorded as UNREVIEWED, and what only Maulik can supply is in NEEDS-MAULIK.md
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && echo "MGR_ENTRIES=$(grep -cE '^## MGR[0-9]' docs/DECISIONS-MERGE.md) TAGGED=$(grep -E '^## MGR[0-9]' docs/DECISIONS-MERGE.md | grep -c UNREVIEWED) NEEDS=$(grep -ci 'revenue share\|D7' NEEDS-MAULIK.md)"
  EXPECT: /MGR_ENTRIES=([1-9][0-9]*) TAGGED=\1 NEEDS=[1-9]/
  EVIDENCE: MGR_ENTRIES=6 TAGGED=6 NEEDS=3
