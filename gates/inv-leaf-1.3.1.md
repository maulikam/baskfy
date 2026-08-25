# Gates: 1.3.1 Costs-and-returns page

Scope: GET costs payload + /me/investments/[id]/costs page showing returns after accrued fees. Collection stays off.

- [x] G1: Costs API tests green
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest services/api/tests/test_curated_costs.py --tb=line 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: ...                                                                      [100%] | 3 passed in 0.61s

- [x] G2: Page file exists with costs-after-fees
  CHECK: rg -n "costs-after-fees" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/app/\(app\)/me/investments/\[id\]/costs/page.tsx
  EXPECT: costs-after-fees
  EVIDENCE: 84:            <div data-testid="costs-after-fees">

- [x] G3: Track B collect not referenced as a live POST from the page
  CHECK: rg -n "fees/collect|/execute" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/app/\(app\)/me/investments/\[id\]/costs/page.tsx || echo NONE
  EXPECT: NONE
  EVIDENCE: NONE
