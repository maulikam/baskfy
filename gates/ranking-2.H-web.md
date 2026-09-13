# Gates: 2.H — web

- [x] G1: Ranking section: mode (Single / Sequential / Composite) with copy that states the difference; term editor (factor combobox limited to rankable factors, preference defaulting from the factor, weight for composite, min/max for target range); family weights (composite); missing-data select; scope (Fixed universe / Filtered results / Within sector) with explanation; new screens default to fixed_universe; component tests.
  CHECK: cd decile-blueprint && pnpm --filter @baskfy/web exec vitest run src/components/screens/__tests__/ranking-section.test.tsx 2>&1 | grep -E "Tests" | tail -1
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: 2026-09-13 CHECK as written -> "Tests  12 passed (12)"; eslint on the 14 changed web files clean; tsc --noEmit shows only the pre-existing FactorOut `preference` errors in kitchen-sink/fixtures.ts and factor-combobox.test.tsx (generated schema regen, leaf 2.G), none in files this leaf touched.

- [x] G2: Factor ranges filter section (incl. "beat NIFTY 500 by ≥ X pp" via excess_ret_*) and regime filter; tests.
  CHECK: cd decile-blueprint && pnpm --filter @baskfy/web exec vitest run src/components/screens/__tests__/factor-ranges.test.tsx 2>&1 | grep -E "Tests" | tail -1
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: 2026-09-13 CHECK as written -> "Tests  7 passed (7)"; affected suites (coverage, filter-chip-bar, url-state, screen-editor, universe-label) 115 passed.

- [ ] G3: Results header always shows universe, as-of, data version, score/engine version, scope and mode from `provenance`; tests.
  CHECK: cd decile-blueprint && pnpm --filter @baskfy/web exec vitest run src/components/screens/__tests__/provenance.test.tsx 2>&1 | grep -E "Tests" | tail -1
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: pending

- [ ] G4: Peek drawer explanation from `/screens/explain`: total + component table (raw, score, weight, contribution), positives, deductions, eligibility, data quality, desk A–F with raw inputs, provenance, rank history today / previous / change; tests.
  CHECK: cd decile-blueprint && pnpm --filter @baskfy/web exec vitest run src/components/screens/__tests__/peek-drawer.test.tsx src/components/screens/__tests__/rank-explanation.test.tsx 2>&1 | grep -E "Tests" | tail -1
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: pending — UNTICKED 2026-09-14: earlier tick was a false pass (rank-explanation.test.tsx does not exist, so vitest ran only peek-drawer tests; nothing in apps/web calls /screens/explain).

- [ ] G5: Presets menu from `/meta/ranking-presets` with status badges; factor combobox shows validation status; Portfolio fit panel (portfolio picker, constraints, per-row decision + reasons, clearly "informational — no orders") separate from the quality ranking; tests.
  CHECK: cd decile-blueprint && pnpm --filter @baskfy/web exec vitest run src/components/screens/__tests__/presets-menu.test.tsx src/components/screens/__tests__/portfolio-fit.test.tsx 2>&1 | grep -E "Tests" | tail -1
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: pending

- [ ] G6: Whole web suite + lint (next typegen, tsc, eslint) green; `next build` succeeds.
  CHECK: cd decile-blueprint && pnpm --filter @baskfy/web test 2>&1 | grep -E "^\s*Tests" | tail -1 && pnpm --filter @baskfy/web lint 2>&1 | tail -2 && pnpm --filter @baskfy/web exec next build 2>&1 | grep -E "Compiled|Failed|error" | head -3
  EXPECT: /passed[\s\S]*Compiled/
  EVIDENCE: pending

- [ ] G7: Looked at in a real browser against a local API with seeded data: composite screen, scope switch changes ranks, peek explanation renders, portfolio fit renders; screenshots saved under docs/ranking/screens/.
  EVIDENCE: pending
