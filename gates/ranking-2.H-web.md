# Gates: 2.H — web

- [x] G1: Ranking section: mode (Single / Sequential / Composite) with copy that states the difference; term editor (factor combobox limited to rankable factors, preference defaulting from the factor, weight for composite, min/max for target range); family weights (composite); missing-data select; scope (Fixed universe / Filtered results / Within sector) with explanation; new screens default to fixed_universe; component tests.
  CHECK: cd decile-blueprint && pnpm --filter @baskfy/web exec vitest run src/components/screens/__tests__/ranking-section.test.tsx 2>&1 | grep -E "Tests" | tail -1
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: 2026-09-13 CHECK as written -> "Tests  12 passed (12)"; eslint on the 14 changed web files clean; tsc --noEmit shows only the pre-existing FactorOut `preference` errors in kitchen-sink/fixtures.ts and factor-combobox.test.tsx (generated schema regen, leaf 2.G), none in files this leaf touched.

- [x] G2: Factor ranges filter section (incl. "beat NIFTY 500 by ≥ X pp" via excess_ret_*) and regime filter; tests.
  CHECK: cd decile-blueprint && pnpm --filter @baskfy/web exec vitest run src/components/screens/__tests__/factor-ranges.test.tsx 2>&1 | grep -E "Tests" | tail -1
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: 2026-09-13 CHECK as written -> "Tests  7 passed (7)"; affected suites (coverage, filter-chip-bar, url-state, screen-editor, universe-label) 115 passed.

- [x] G3: Results header always shows universe, as-of, data version, score/engine version, scope and mode from `provenance`; tests.
  CHECK: cd decile-blueprint && pnpm --filter @baskfy/web exec vitest run src/components/screens/__tests__/provenance.test.tsx 2>&1 | grep -E "Tests" | tail -1
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: 2026-09-14 CHECK -> "Tests  7 passed (7)"; ProvenanceHeader in the results panel reads only `provenance` (legacy-sql and "Not used" desk score shown plainly); eslint clean on changed files apart from two pre-existing screen-editor.tsx errors (lines not touched); tsc --noEmit clean.

- [x] G4: Peek drawer explanation from `/screens/explain`: total + component table (raw, score, weight, contribution), positives, deductions, eligibility, data quality, desk A–F with raw inputs, provenance, rank history today / previous / change; tests.
  CHECK: cd decile-blueprint && pnpm --filter @baskfy/web exec vitest run src/components/screens/__tests__/peek-drawer.test.tsx src/components/screens/__tests__/rank-explanation.test.tsx 2>&1 | grep -E "Tests" | tail -1
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: 2026-09-14 CHECK as written -> "Tests  21 passed (21)" (rank-explanation 12, peek-drawer 9, both files ran); POST /screens/explain now returns rank_history (today, previous from the same definition ranked on the previous trading day, change) and desk components with ext_over_20dma, score_version and per-grade ranges; core spec tests plus API tests with hand-set data for two sessions (explain/selection 29, test_api_run + test_screener_db 200, all passed); ruff, ruff format, mypy clean; make client run, api-client 177 passed; eslint clean on changed web files; tsc --noEmit clean.

- [x] G5: Presets menu from `/meta/ranking-presets` with status badges; factor combobox shows validation status; Portfolio fit panel (portfolio picker, constraints, per-row decision + reasons, clearly "informational — no orders") separate from the quality ranking; tests.
  CHECK: cd decile-blueprint && pnpm --filter @baskfy/web exec vitest run src/components/screens/__tests__/presets-menu.test.tsx src/components/screens/__tests__/portfolio-fit.test.tsx 2>&1 | grep -E "Tests" | tail -1
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: 2026-09-14 CHECK -> "Tests  21 passed (21)" (presets-menu 11 incl. factor list validation badges, portfolio-fit 10); presets validated as ScreenDefinition patches over the default; Portfolio fit posts /screens/selection with portfolio_id or holdings [], lists holdings_without_quantity, labelled "Informational only — no orders", never touches the results table; no-jargon scan shows only the pre-existing column-display.ts "Book" hits; tsc --noEmit clean.

- [x] G6: Whole web suite + lint (next typegen, tsc, eslint) green; `next build` succeeds.
  CHECK: cd decile-blueprint && pnpm --filter @baskfy/web test 2>&1 | grep -E "^\s*Tests" | tail -1 && pnpm --filter @baskfy/web lint 2>&1 | tail -2 && pnpm --filter @baskfy/web exec next build 2>&1 | grep -E "Compiled|Failed|error" | head -3
  EXPECT: /passed[\s\S]*Compiled/
  EVIDENCE: 2026-09-14 whole web vitest suite 216 files / 3,385 passed; lint (next typegen, tsc, eslint) exit 0; next build with the four build args from tools/deploy/push-images.sh (NEXT_PUBLIC_SITE_URL, NEXT_PUBLIC_API_ORIGIN, NEXT_PUBLIC_API_URL = https://staging.baskfy.com[/api/v1], NEXT_PUBLIC_DESK_URL = https://desk.staging.baskfy.com) exit 0 -> "Compiled with warnings in 12.5s", types valid, "Generating static pages (39/39)"; the only warnings are the openapi-fetch process.versions Edge Runtime notice via packages/api-client; no fixes needed; .next removed afterwards.

- [x] G7: Looked at in a real browser against a local API with seeded data: composite screen, scope switch changes ranks, peek explanation renders, portfolio fit renders; screenshots saved under docs/ranking/screens/.
  EVIDENCE: 2026-09-14 Chrome against local uvicorn + next dev on a throwaway baskfy_dev_g7 (migrated to 0047, `seed e2e`, backfill-ranking for 17/18 Aug 2026, a 4-holding test portfolio), DRY_RUN=true, forged local session for the seeded e2e account: composite screen (3 terms, NIFTY 500, 1-yr return >= 30%, 97 matches) with header "Ranking method ranking-2.0.0 · Compared against Fixed universe · Ranking mode Composite"; switching scope to Filtered results reordered ranks (#2 DIVISLAB -> FEDERALBNK, LAURUSLABS #6 -> #3, scores changed); peek for TITAN showed total 90.4, the term table (raw, score, weight, contribution), what helps, filters, data checks, desk A-F, rank over time and provenance; Portfolio fit showed the informational-only notice, the picker, constraints, Keep 1 / Would add 14 / Would remove 3 with per-row reasons; no console errors. Fixed: the composite column header showed the raw key "composite_score" (added a COLUMN_DISPLAY entry "Composite score"; column-display + results-panel vitest 58 passed, tsc and eslint clean). Screenshots: docs/ranking/screens/g7-01-composite-provenance.jpg, g7-02a-ranks-fixed-universe.jpg, g7-02b-ranks-filtered-results.jpg, g7-03a-peek-explanation.jpg, g7-03b-peek-desk-and-rank-history.jpg, g7-04a-portfolio-fit-panel.jpg, g7-04b-portfolio-fit-decisions.jpg. API, web, database, containers and Docker stopped afterwards.
