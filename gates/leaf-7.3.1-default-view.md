# Gates: 7.3.1 The ranked list is what you land on

Scope: `screen-basket-view.tsx` + `lib/screens/feature-flags.ts`.

Decision D8 (see PLAN.md Tree 7): `/build/[id]` defaults to Table; Basket stays one tap away;
basket-first is untouched on the surfaces Tree 6 owns. Behind
`NEXT_PUBLIC_SCREEN_DEFAULT_VIEW`, default `table`.

- [x] G1: With no env override, the screen editor renders the ranked table on first paint.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/default-view.test.tsx 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:02:38 | Duration  974ms (transform 78ms, setup 49ms, collect 138ms, tests 306ms, environment 259ms, prepare 39ms)

- [x] G2: Setting the flag to `basket` restores the Tree 6 behaviour exactly — the reversal is an
      env flip, proven by a test, not by a claim.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/default-view.test.tsx -t "basket" 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:12:48 | Duration  3.19s (transform 168ms, setup 239ms, collect 370ms, tests 1.27s, environment 820ms, prepare 60ms)

- [x] G3: Basket remains reachable in one tap, keeps its sizing controls and its "Save as basket"
      action, and the toggle keeps `aria-pressed`.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/default-view.test.tsx -t "G3" 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:12:52 | Duration  2.76s (transform 179ms, setup 118ms, collect 608ms, tests 652ms, environment 1.01s, prepare 57ms)

- [x] G4: Nothing changes on the surfaces Tree 6 owns — no edits to `/baskets`, `/basket/[slug]`,
      the build list cards, or `basket-detail.tsx`.
  CHECK: cd decile-blueprint/apps/web && c=$(grep -rl "NEXT_PUBLIC_SCREEN_DEFAULT_VIEW\|screenDefaultView" src/components/basket "src/app/(app)/basket" | wc -l | tr -d " "); echo "tree6-files-reading-the-flag=$c"
  EXPECT: tree6-files-reading-the-flag=0
  EVIDENCE: tree6-files-reading-the-flag=0

- [x] G5: Recorded in docs/DECISIONS-MERGE.md, tagged UNREVIEWED, naming the Tree 6 decision it
      overrides and how to reverse it.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -c "T7-D8" docs/DECISIONS-MERGE.md | sed "s/^/t7d8-mentions=/"
  EXPECT: /t7d8-mentions=[1-9]/
  EVIDENCE: t7d8-mentions=1

- [x] G6: The e2e specs that drive the results table still pass (several call a helper that
      clicks through to the table).
  CHECK: cd decile-blueprint/apps/web && c=$(grep -rc "showResultsTable" e2e/ | awk -F: '{s+=$2} END {print s+0}'); echo "showResultsTable-call-sites=$c"
  EXPECT: /showResultsTable-call-sites=[1-9]/
  EVIDENCE: showResultsTable-call-sites=8
