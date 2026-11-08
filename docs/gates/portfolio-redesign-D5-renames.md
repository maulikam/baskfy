# Gates: leaf D5-renames

Scope: §8 language renames + boilerplate removal — acceptance criterion 7

Written by the parent before fan-out. The leaf agent proves its own work; the parent
re-runs these checks independently — that verification hierarchy is the whole point of
orchestrated mode.

---

- [x] G1: A scanner test exists and fails if any §8 jargon reappears in user-visible strings.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/__tests__/no-jargon.test.ts 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  9 passed (9)

- [x] G2: The web unit suite did not regress.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  1699 passed (1699)

<!--
A checked box with EVIDENCE still "pending" counts as UNMET.
If a gate becomes impossible: add `ABANDON: G<n> <reason>` and say so in the report.
-->
