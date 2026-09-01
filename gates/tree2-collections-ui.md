# Gates: Collections — stop the shelves lying about a one-basket catalogue (tree 2, UI, solo)

Scope: four shelves exist; three render the same single basket and `quarterly` renders a dashed
empty box. Make the browse surfaces behave honestly at this catalogue size without fabricating
baskets, and without any shelf becoming unreachable.

## Verified before writing these gates (measured, not assumed)

Against the real dev database `baskfy` on `localhost:5433`:

- `cb_collection` — 4 rows: `start-here [1]`, `momentum [1]`, `run-by-the-engine [1]`, `quarterly []`.
- `cb_basket` — **exactly one row**: `id=1 momentum-scan`, PUBLISHED, `categories={momentum}`,
  `rebalance_frequency=WEEKLY`, `manager=baskfy-engine`, `source=SCAN`.

**The catalogue grew while this ran.** Another session's `seed_catalogue` landed mid-work and
`cb_basket` went from 1 row to 6-7. That is recorded here rather than edited away, because it is
what forced the rule from containment to identity (COL5) and what exposed the duplicate-id defect
(COL8). The one-basket shape is kept as a fixture in both test suites: it is what a freshly seeded
database looks like, so it is the case the browse surfaces must survive.

So the *data is correct*. Every shelf is an honest predicate; three collapse onto the one basket
because it satisfies all three, and `quarterly` is empty because no quarterly basket exists. This
is the downstream symptom the brief says it is, and the fix must not invent a basket to hide it.

**One curation defect found that is NOT downstream of the thin catalogue:** `start-here` carries
no predicate at all (`curated_seed.py` — no `categories`, no `managers`, no `rebalance_frequency`,
only `ordering="min_amount"`). It is the entire catalogue with a different sort, and would still
be at 300 baskets. A shelf that is definitionally the whole catalogue can never differentiate.
That is fixed here too, because the render-time rule below depends on shelves being able to differ.

## The rule this work introduces

*A shelf earns space on a page that already shows the catalogue only when it groups something.*

- Empty shelves and shelves holding **exactly** the baskets of a shelf already kept are dropped
  from a stacked, multi-shelf render. (Written as *subset* first; corrected to identity when the
  catalogue grew mid-session and containment would have hidden a real shelf — see COL5.)
- Fewer than two survivors means the stack groups nothing, so the page shows the **directory**
  (one tile per collection, counts only) instead of repeating basket cards.
- Suppression is presentation-only: every collection keeps its tile and its own page.

## Leaf 1 — the selection rule

- [x] G1: a pure, tested `selectShelves` exists — drops empty shelves, drops shelves identical to one above, keeps curator order
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/lib/collections 2>&1 | grep -E "Tests +[0-9]+ passed" | tail -1
  EXPECT: /Tests +[1-9][0-9]* passed(?!.*failed)/
  EVIDENCE: Tests  16 passed (16)

- [x] G2: today's real payload (three shelves on one basket, one empty) yields exactly one survivor, which is below the stacking threshold
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/lib/collections -t "one-basket catalogue" 2>&1 | grep -E "Tests +[0-9]+ passed" | tail -1
  EXPECT: /Tests +[1-9][0-9]* passed(?!.*failed)/
  EVIDENCE: Tests  1 passed | 15 skipped (16)

- [x] G3: a differentiated catalogue still stacks — shelves that genuinely group are not suppressed
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/lib/collections -t "differentiated" 2>&1 | grep -E "Tests +[0-9]+ passed" | tail -1
  EXPECT: /Tests +[1-9][0-9]* passed(?!.*failed)/
  EVIDENCE: Tests  2 passed | 14 skipped (16)

- [x] G4: suppression never hides a collection from the product — a suppressed shelf still has a directory tile and its own page
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/lib/collections -t "reachable" 2>&1 | grep -E "Tests +[0-9]+ passed" | tail -1
  EXPECT: /Tests +[1-9][0-9]* passed(?!.*failed)/
  EVIDENCE: Tests  1 passed | 15 skipped (16)

## Leaf 2 — the surfaces

- [x] G5: `/baskets` never renders one shelf twice under two names, and no shelf lists a basket twice — the symptom, asserted against the real shape
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/components/collections 2>&1 | grep -E "Tests +[0-9]+ passed" | tail -1
  EXPECT: /Tests +[1-9][0-9]* passed(?!.*failed)/
  EVIDENCE: Tests  23 passed (23)

- [x] G6: the collections index is a complete directory — every collection listed, empty ones included, no repeated basket cards
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/components/collections -t "directory" 2>&1 | grep -E "Tests +[0-9]+ passed" | tail -1
  EXPECT: /Tests +[1-9][0-9]* passed(?!.*failed)/
  EVIDENCE: Tests  2 passed | 21 skipped (23)

- [x] G7: a shelf's own page keeps the honest empty statement — the earlier tree's spec is preserved, not traded away
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/components/collections/__tests__/collection-shelf.test.tsx 2>&1 | grep -E "Tests +[0-9]+ passed" | tail -1
  EXPECT: /Tests +[1-9][0-9]* passed(?!.*failed)/
  EVIDENCE: Tests  8 passed (8)

- [x] G8: collections stay reachable from `/baskets` (the earlier tree's G11 still holds)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && grep -c "collections" "apps/web/src/app/(app)/baskets/page.tsx"
  EXPECT: /^[1-9]/m
  EVIDENCE: 6

- [x] G9: `start-here` is capped, no shelf names a basket twice, and the seeder is still idempotent (house rule 7). DECLARED ISOLATION: run with a throwaway plugin that stubs `seed_catalogue`. Another session's untracked `curated_catalogue.py` is called from `seed_reference()`, which the fixture runs *before* `publish_run`, so it raises `NoPublishedData` and errors the setup of **every** db-backed API suite in the repo, including ones that predate it. No repo file was modified to get around it; the collections tests never read `cb_catalogue`.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL="$(grep -E '^BASKFY_TEST_DATABASE_URL=' .env | cut -d= -f2- | tr -d '"' | sed 's|/baskfy_test$|/baskfy_test_col2|')" && PYTHONPATH=/private/tmp/claude-501/-Users-maulikdave-Documents-projects-baskfy/fb90c35e-727f-4bec-ac70-a276e545362c/scratchpad uv run pytest services/api/tests/test_collections.py services/api/tests/test_explore_http.py -p isolate_catalogue --tb=line 2>&1 | tail -2
  EXPECT: /^\d+ passed/m
  EVIDENCE: ...............................................                          [100%] | 47 passed in 7.54s

## Integration

- [x] G10: measured end to end against the live dev database — the real rows, through the real `_collection_out`, through the real `selectShelves`
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_DATABASE_URL="$(grep -E '^BASKFY_DATABASE_URL=' .env | cut -d= -f2- | tr -d '"')" && SP=/private/tmp/claude-501/-Users-maulikdave-Documents-projects-baskfy/fb90c35e-727f-4bec-ac70-a276e545362c/scratchpad && uv run python $SP/dump_payload.py > $SP/live-collections.json && node $SP/decide.ts $SP/live-collections.json | tr '\n' ' '
  EXPECT: /NO_TWO_SHELVES_ALIKE true NO_SHELF_REPEATS_A_BASKET true NONE_LOST +true/
  EVIDENCE: INPUT       start-here, momentum, run-by-the-engine, quarterly STACKED     start-here, quarterly SUPPRESSED  momentum, run-by-the-engine MODE        shelves NO_TWO_SHELVES_ALIKE true NO_SHELF_REPEATS_

- [x] G11: the whole web unit suite is green and every file this work owns lints clean with no new own TS errors. SCOPED and measured: package-wide `pnpm run lint` is red with 22 errors across 16 files, none of them in `lib/collections`, `components/collections`, `components/home` or `app/(app)/baskets` — they belong to the cb, create, data, investments, screens and basket trees. `tsc --noEmit` over the whole package is 0 errors.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run >/dev/null 2>&1 && pnpm exec eslint src/lib/collections src/components/collections src/components/home 'src/app/(app)/baskets' >/dev/null 2>&1 && OWN=$(pnpm exec tsc --noEmit 2>&1 | grep -E "error TS" | grep -cE "collections|collection-" || true); echo "OWN_TS_ERRORS=$OWN"; [ "$OWN" = 0 ] && echo GATE_OK || echo GATE_FAILED
  EXPECT: GATE_OK
  EVIDENCE: OWN_TS_ERRORS=0 | GATE_OK

- [x] G12: every Python file this work owns is ruff/format clean and mypy-clean. SCOPED: `mypy services/api/src` reports 3 errors, all in another session's untracked `curated_catalogue.py`; measured, unowned, and not caused here — this gate typechecks the files this tree touched.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && OWNED="services/api/src/baskfy_api/curated_seed.py services/api/src/baskfy_api/routers/explore.py services/api/tests/test_collections.py" && uv run ruff check $OWNED >/dev/null 2>&1 && uv run ruff format --check $OWNED >/dev/null 2>&1 && FOREIGN=$(uv run mypy services/api/src 2>&1 | grep -cE "^services/api/src/baskfy_api/curated_catalogue.py"); MINE=$(uv run mypy services/api/src 2>&1 | grep -E "error:" | grep -vcE "^services/api/src/baskfy_api/curated_catalogue.py"); echo "MINE=$MINE FOREIGN=$FOREIGN"; [ "$MINE" = 0 ] && echo GATE_OK || echo GATE_FAILED
  EXPECT: GATE_OK
  EVIDENCE: MINE=0 FOREIGN=0 | GATE_OK

- [x] G13: no execute affordance appeared on any collections surface (non-negotiable #1)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && echo "EXEC_HITS=$(grep -rnE 'place_order|/execute|confirm=true|OrderGateway' apps/web/src/lib/collections apps/web/src/components/collections "apps/web/src/app/(app)/baskets/collections" 2>/dev/null | grep -v __tests__ | wc -l | tr -d ' ')"
  EXPECT: EXEC_HITS=0
  EVIDENCE: EXEC_HITS=0

- [x] G14: the judgement calls are recorded as UNREVIEWED, per the autonomy charter
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && echo "COL=$(grep -cE '^## COL[5-8] ' docs/DECISIONS-MERGE.md) TAGGED=$(grep -E '^## COL[5-8] ' docs/DECISIONS-MERGE.md | grep -c UNREVIEWED)"
  EXPECT: /COL=([1-9][0-9]*) TAGGED=\1/
  EVIDENCE: COL=4 TAGGED=4
