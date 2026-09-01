# Gates: leaf D2-overview

Scope: §6 the Overview page — the single consolidated screen

Written by the parent. The leaf proves its own work; the parent re-runs these independently.

---

- [x] G1: The Overview components exist and their tests pass.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  106 passed (106)

- [x] G2: The whole web suite did not regress.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  1800 passed (1800)

- [x] G3: The app typechecks — no leaf may leave the tree red.
  CHECK: cd decile-blueprint/apps/web && pnpm exec tsc --noEmit && echo TSC CLEAN
  EXPECT: /TSC CLEAN/
  EVIDENCE: TSC CLEAN

<!--
A checked box with EVIDENCE still "pending" counts as UNMET.
If a gate becomes impossible: add `ABANDON: G<n> <reason>` and say so in the report.
-->
