# Gates: Tree 4 root — product integrity

Scope: integration across leaves A–D. Plan: `PLAN-TREE4-INTEGRITY.md`.

- [x] R1: One investments journey is reachable. The legacy tree no longer serves a second live
      page for the same id.
  CHECK: bash gates/tree4-check-routes.sh 2>&1 | tail -4
  EXPECT: ONE_JOURNEY_OK
  EVIDENCE: ONE_JOURNEY_OK. LEGACY_PAGE_BODIES=0, APP_LINKS_TO_LEGACY=0, and all five legacy paths 308 to their /me twin.

- [x] R2: The public landing page renders its sample screen instead of an error.
  CHECK: bash gates/tree4-check-landing.sh 2>&1 | tail -4
  EXPECT: LANDING_OK
  EVIDENCE: LANDING_OK. LANDING_HTTP=200, ERROR_TEXT=absent, SAMPLE_ROWS=9 (header + 8).

- [x] R3: Nothing in this tree broke the web suite; it passes at or above the pre-tree count.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run 2>&1 | grep -oE "Tests +[0-9]+ passed" | tail -1
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Test Files 83 passed (83) · Tests 1486 passed (1486) · 0 failed.

- [x] R4: Every file this tree touched is lint- and type-clean.
  CHECK: bash gates/tree4-check-lint.sh 2>&1 | tail -4
  EXPECT: TREE4_LINT_OK
  EVIDENCE: TREE4_LINT_OK — eslint clean on all 7 edited files and tsc --noEmit clean across the package.

- [x] R5: The two items that did not reproduce are reported as measurements, not quietly dropped
      and not padded into invented work.
  EVIDENCE: Both non-reproducing items are reported as measurements in PLAN-TREE4-INTEGRITY.md and in the leaf gates: B measured 59 files handling empty collections (not 9), D measured 0 TODO/FIXME in apps/web/src and 1 repo-wide outside tests (not 31). Neither was padded into invented work.
