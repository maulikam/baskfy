# Gates: 1.1.1 Mark-as-invested

Scope: POST /cb/investments/mark creates ACTIVE cb_investment + holdings + PLANNED BUY batch + uncollected fee; GET list/detail/fees; UI confirm form. No execute.

- [x] G1: OpenAPI has POST /api/v1/cb/investments/mark and GET /api/v1/cb/investments
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run python -c "from baskfy_api.app import create_app; p=create_app().openapi()['paths']; assert 'post' in p['/api/v1/cb/investments/mark']; assert 'get' in p['/api/v1/cb/investments']; print('MARK_ROUTES')"
  EXPECT: MARK_ROUTES
  EVIDENCE: MARK_ROUTES

- [x] G2: API tests green (mark, list, 409 duplicate, confirmed required, no broker tokens)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest services/api/tests/test_curated_investments.py --tb=line 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: ......                                                                   [100%] | 6 passed in 0.64s

- [x] G3: Router source has no OrderGateway / place_order / /execute
  CHECK: rg -n "OrderGateway|place_order|/execute|kiteconnect" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/services/api/src/baskfy_api/routers/curated_investments.py /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/services/api/src/baskfy_api/curated_investments.py || echo NONE
  EXPECT: NONE
  EVIDENCE: NONE

- [x] G4: Mark form + read-only vitest green
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/lib/investments/__tests__/read-only.test.ts src/components/cb/__tests__/mark-invested-form.test.tsx --reporter=dot 2>&1 | tail -20
  EXPECT: passed
  EVIDENCE: Start at  19:45:25 | Duration  1.63s (transform 125ms, setup 354ms, collect 206ms, tests 158ms, environment 1.33s, prepare 193ms)

- [x] G5: data-testid mark-invested-form exists in the form component
  CHECK: rg -n "mark-invested-form" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/cb/mark-invested-form.tsx
  EXPECT: mark-invested-form
  EVIDENCE: 80:      data-testid="mark-invested-form"
