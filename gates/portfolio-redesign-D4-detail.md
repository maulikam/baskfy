# Gates: leaf D4-detail

Scope: §7 the portfolio detail page

Written by the parent. The leaf proves its own work; the parent re-runs these independently.

---

- [x] G1: The detail components exist and their tests pass.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/__tests__ 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  82 passed (82)

- [x] G2: The app typechecks and the web suite did not regress.
  CHECK: cd decile-blueprint/apps/web && pnpm exec tsc --noEmit && pnpm exec vitest run 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  1841 passed (1841)

<!--
A checked box with EVIDENCE still "pending" counts as UNMET.
If a gate becomes impossible: add `ABANDON: G<n> <reason>` and say so in the report.
-->
