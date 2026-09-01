# Gates: leaf D3-onboarding

Scope: §6.6/§6.7 unallocated-first onboarding and the grouping flow

Written by the parent. The leaf proves its own work; the parent re-runs these independently.

---

- [x] G1: The organize library and its components pass.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/portfolio/__tests__/organize.test.ts src/components/portfolio/__tests__/unallocated-organize.test.tsx 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  33 passed (33)

- [x] G2: Criterion 8: the empty state leads to Connect your broker, never the catalogue.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run -t 'broker' src/components/portfolio/__tests__/unallocated-organize.test.tsx 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  3 passed | 12 skipped (15)

- [x] G3: §4.2 whole holdings only — the UI has no partial-quantity input at all.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run -t 'partial' src/components/portfolio/__tests__/unallocated-organize.test.tsx 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  1 passed | 14 skipped (15)

<!--
A checked box with EVIDENCE still "pending" counts as UNMET.
If a gate becomes impossible: add `ABANDON: G<n> <reason>` and say so in the report.
-->
