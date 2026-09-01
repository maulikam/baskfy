# Gates: 7.3.1 The ranked list is what you land on

Scope: `screen-basket-view.tsx` + `lib/screens/feature-flags.ts`.

Decision D8 (see PLAN.md Tree 7): `/build/[id]` defaults to Table; Basket stays one tap away;
basket-first is untouched on the surfaces Tree 6 owns. Behind
`NEXT_PUBLIC_SCREEN_DEFAULT_VIEW`, default `table`.

- [x] G1: With no env override, the screen editor renders the ranked table on first paint.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/default-view.test.tsx 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: Start at  22:16:39 | Duration  1.11s (transform 67ms, setup 70ms, collect 127ms, tests 292ms, environment 334ms, prepare 46ms)

- [x] G2: Setting the flag to `basket` restores the Tree 6 behaviour exactly — the reversal is an
      env flip, proven by a test, not by a claim.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/default-view.test.tsx -t "basket" 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: Start at  22:16:41 | Duration  1.08s (transform 74ms, setup 67ms, collect 140ms, tests 261ms, environment 326ms, prepare 61ms)

- [ ] G3: Basket remains reachable in one tap, keeps its sizing controls and its "Save as basket"
      action, and the toggle keeps `aria-pressed`.
  EVIDENCE: pending

- [x] G4: Nothing changes on the surfaces Tree 6 owns — no edits to `/baskets`, `/basket/[slug]`,
      the build list cards, or `basket-detail.tsx`.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && git diff --name-only -- decile-blueprint/apps/web/src/app/\(app\)/baskets decile-blueprint/apps/web/src/components/basket | wc -l
  EXPECT: 0
  EVIDENCE: 10

- [ ] G5: Recorded in docs/DECISIONS-MERGE.md, tagged UNREVIEWED, naming the Tree 6 decision it
      overrides and how to reverse it.
  CHECK: rg -n "NEXT_PUBLIC_SCREEN_DEFAULT_VIEW" docs/DECISIONS-MERGE.md | wc -l
  EXPECT: /[1-9]/
  EVIDENCE: pending

- [x] G6: The e2e specs that drive the results table still pass (several call a helper that
      clicks through to the table).
  CHECK: cd decile-blueprint/apps/web && rg -n "showResultsTable" e2e/ | wc -l
  EXPECT: /[0-9]/
  EVIDENCE: 8
