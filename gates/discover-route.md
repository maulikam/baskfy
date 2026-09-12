# gates/discover-route.md — three API paths a page-tree rename carried off with it (Agent D)

**Goal.** `apps/web/src/lib/basket/fetch.ts` fetched `/discover` and `/discover/plan`, which the
API does not serve. Establish which side drifted rather than guessing, fix the smaller side, and
add a static check that would have caught the whole **class** — every path this app hands to a
fetch helper, against `openapi.json` — so the next rename cannot do it again quietly.

**What was established.** Both sides' history says the same thing. The API's `"/baskets"` string
has not changed since it was written (`8311de2`, M21/M22); the web's `"/discover"` and
`"/api/v1/cb/discover"` were both **introduced by one commit, M48 `6195b57`, 27 Aug 2026**, which
renamed the page tree `app/(app)/baskets/` → `app/(app)/discover/` (three directory renames in
`--find-renames`). The **UI** rename is the decision — `lib/nav.ts` still carries the
`/baskets → /discover` redirects. The API path strings were collateral: M48's message enumerates
four bug reports and three causes and never mentions an API path or an endpoint. Under the root
`CLAUDE.md` rule that is the "no such commit → it may genuinely be a bug" branch, so the web is
the side that moved and the web is the side that is fixed.

**`/explore` is not the answer, and it looks like it.** `/explore` is a *former UI route* that
`lib/nav.ts` redirects to `/discover`, and *also* a live API resource — the curated catalog, read
by `lib/explore/fetch.ts`. It is a different resource from the momentum basket `fetchBasket`
wants, and it does not return `BasketOut`. Repointing the basket fetcher there would have
compiled, returned JSON and been wrong.

**They are not dead code.** All three helpers have live callers (D3). The two read pages have
rendered "There is no featured basket to show yet" and "The desk has recorded no plans yet" since
27 Aug, and the save button on `/create` has failed, because each wrapper turns a non-OK response
into a domain error the page renders as an empty state. **A 404 and an empty database are the same
pixels** — which is why this survived two weeks inside a commit whose own subject line is "the
empty pages were an empty database".

**A third casualty was found while looking.** The same commit moved `POST /api/v1/cb/baskets` to
`/api/v1/cb/discover` in `lib/create/fetch.ts`. Same commit, same cause, same fix.

**What was deliberately NOT done.** No route was added to the API, no alias, no redirect layer —
two names for one resource is how this happens again. `docs/07` and `EXPECTED_PATHS` in
`services/api/tests/test_api_artifacts.py` are unchanged because the API is unchanged; all three
paths are already listed there (D5). `/twt/backtest` is the same class and was **not** fixed here —
it is the TWT sleeve's route and `routers/twt.py` is another agent's file. It was registered in
the class test's `KNOWN_UNSERVED` instead, asserted in both directions so it could not be
forgotten; **the route landed the same afternoon and the register self-cleaned**, so the register
is now empty and every path this app fetches is a path the API serves (D6a).

The decision is written up in `docs/DECISIONS-MERGE.md` §D-ROUTE, tagged `⚠ UNREVIEWED`.

Run from the repo root, `/Users/maulikdave/Documents/projects/baskfy`. CHECKs use absolute paths
so the row is portable. Read-only against the box; nothing was deployed.

---

## The ledger

- [x] **D1: The API has never served `/discover` anything, and does serve all three targets.**
      Asserted against the **live** app's route table, not the checked-in `openapi.json` — which
      another agent is regenerating right now, and a gate that reads a file someone else is
      rewriting is a gate about the file.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentD uv run python -c "from baskfy_api.app import create_app; p=create_app().openapi()['paths']; print('DISCOVER_ROUTES=%d' % len([x for x in p if '/discover' in x])); print('\n'.join(sorted(x for x in p if x in ('/api/v1/baskets','/api/v1/baskets/plan','/api/v1/cb/baskets'))))"
  EXPECT: /^DISCOVER_ROUTES=0$[\s\S]*^\/api\/v1\/baskets$[\s\S]*^\/api\/v1\/baskets\/plan$[\s\S]*^\/api\/v1\/cb\/baskets$/m
  EVIDENCE: `DISCOVER_ROUTES=0`, then `/api/v1/baskets`, `/api/v1/baskets/plan`,
  `/api/v1/cb/baskets`. The routes the web wanted were there the whole time.

- [x] **D2: The web is the side that drifted, in one commit, with no reason given.**
      Both broken strings were *introduced* by `6195b57`; the API's own `"/baskets"` literal has
      not been touched since `8311de2` wrote it. The same commit renamed the page directory three
      times, and its message mentions no API path, endpoint or `/api/v1` anywhere.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && { echo "WEB_BASKET_PATH_FROM=$(git log --format=%h -S'"/discover"' -- decile-blueprint/apps/web/src/lib/basket/fetch.ts | tail -1)"; echo "WEB_CREATE_PATH_FROM=$(git log --format=%h -S'"/api/v1/cb/discover"' -- decile-blueprint/apps/web/src/lib/create/fetch.ts | tail -1)"; echo "API_BASKETS_LITERAL_FROM=$(git log --format=%h -S'"/baskets"' -- decile-blueprint/services/api/src/baskfy_api/routers/baskets.py | tail -1)"; echo "UI_DIR_RENAMES=$(git show 6195b57 --stat --find-renames | grep -c '{baskets => discover}')"; echo "MSG_MENTIONS_API_PATH=$(git log -1 --format=%B 6195b57 | grep -ci 'api/v1\|api path\|endpoint')"; }
  EXPECT: /^WEB_BASKET_PATH_FROM=6195b57$[\s\S]*^WEB_CREATE_PATH_FROM=6195b57$[\s\S]*^API_BASKETS_LITERAL_FROM=8311de2$[\s\S]*^UI_DIR_RENAMES=3$[\s\S]*^MSG_MENTIONS_API_PATH=0$/m
  EVIDENCE: `6195b57`, `6195b57`, `8311de2`, `3`, `0`. One side moved; the commit that moved it
  was about seeding a staging database and a nested Radix popover.

- [x] **D3: The functions are live, so a screen was broken — this is not dead code to delete.**
      Three call sites in real pages, none of them tests.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && grep -rn "fetchBasket()\|fetchLatestPlan()\|createPrivateBasket(" apps/web/src/app apps/web/src/components | grep -v "__tests__" | sed 's|:.*await |  <- |'
  EXPECT: /discover\/plan\/page\.tsx[\s\S]*discover\/featured\/page\.tsx[\s\S]*create\/create-basket-form\.tsx/
  EVIDENCE: `app/(app)/discover/plan/page.tsx:30`, `app/(app)/discover/featured/page.tsx:34`,
  `components/create/create-basket-form.tsx:113`. Had they been dead, the honest fix was deletion.

- [x] **D4: The fix is five lines on the web side and there is no `/discover` API path left.**
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && grep -hn '"/baskets"\|"/baskets/plan"\|"/api/v1/cb/baskets"' apps/web/src/lib/basket/fetch.ts apps/web/src/lib/create/fetch.ts && echo "REMAINING_DISCOVER_API_PATHS=$(grep -rn 'readJson("/discover\|"/api/v1/cb/discover"' apps/web/src | wc -l | tr -d ' ')"
  EXPECT: /readJson\("\/baskets"\)[\s\S]*readJson\("\/baskets\/plan"\)[\s\S]*^REMAINING_DISCOVER_API_PATHS=0$/m
  EVIDENCE: `readJson("/baskets")`, `readJson("/baskets/plan")`, `"/api/v1/cb/baskets"`, and
  `REMAINING_DISCOVER_API_PATHS=0`. Every other `/discover` in the tree is a UI route, which is
  what it should be.

- [x] **D5: The API and its contract are untouched — no new route, no alias, no docs/07 edit.**
      The three paths were already in `EXPECTED_PATHS`, which is why adding a route would have
      been the larger and wronger fix: it would have needed a `docs/07` entry for a resource that
      already exists under another name, and `test_nothing_undocumented_is_exposed` would have
      been right to refuse it.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && echo "API_FILES_CHANGED_BY_ME=$(git diff --name-only -- decile-blueprint/services/api/src/baskfy_api/routers/baskets.py decile-blueprint/services/api/src/baskfy_api/routers/curated_create.py decile-blueprint/services/api/src/baskfy_api/routers/explore.py decile-blueprint/docs/07-api-spec.md | wc -l | tr -d ' ')" && grep -n '"/baskets": {"get"}\|"/baskets/plan": {"get"}\|"/cb/baskets": {"post"}' decile-blueprint/services/api/tests/test_api_artifacts.py
  EXPECT: /^API_FILES_CHANGED_BY_ME=0$[\s\S]*"\/baskets": \{"get"\}[\s\S]*"\/baskets\/plan": \{"get"\}[\s\S]*"\/cb\/baskets": \{"post"\}/m
  EVIDENCE: `API_FILES_CHANGED_BY_ME=0`, and all three already present in `EXPECTED_PATHS`.

- [x] **D6: The class test exists, runs in CI, and sees the calls it claims to scan.**
      `apps/web/src/lib/api/__tests__/served-paths.test.ts`, picked up by
      `pnpm --filter @baskfy/web run test` — CI's `web` job (`.github/workflows/ci.yml`). It scans
      every `.ts`/`.tsx` under `apps/web/src` with comments blanked by a small tokenizer (half the
      prose in this repo names a route, and `http://` inside a string is not a comment), for the
      two shapes an API path takes here: a literal containing `/api/v1`, and a string handed to
      one of the ~19 modules' private `readJson`/`readOrNull`/`post` wrappers around
      `` `${serverApiOrigin()}/api/v1${path}` ``. Wrapper detection closes over delegation, so
      `readOrNull → readJson` is followed. Its first assertion is that it found >150 sites, because
      the failure mode of every source-scanning test is passing by seeing nothing.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/lib/api/__tests__/served-paths.test.ts 2>&1 | tail -9 && grep -c "pnpm --filter @baskfy/web run test" ../../.github/workflows/ci.yml
  EXPECT: /served-paths\.test\.ts \(4 tests\)[\s\S]*Tests {2}4 passed \(4\)[\s\S]*^1$/m
  EVIDENCE: `✓ src/lib/api/__tests__/served-paths.test.ts (4 tests)`, `Tests 4 passed (4)`, and
  the CI line present once.

- [x] **D6a: `KNOWN_UNSERVED` is a register, not an allowlist — and it is now empty.**
      It was written with one entry, `/twt/backtest` (`lib/twt/fetch.ts` `fetchBacktest`), the
      third casualty the audit found and the one belonging to the TWT sleeve rather than to this
      lane. **The route landed the same afternoon and the register self-cleaned within minutes**:
      `keeps no stale entries` went red, the entry was deleted, and every path this app fetches is
      now a path the API serves. That is the mechanism, demonstrated rather than asserted — an
      entry cannot outlive its bug in either direction (`keeps no stale entries` if the route
      arrives, `keeps no entry for a path nothing fetches any more` if the call goes away), so it
      can never be used to make the suite green.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && python3 -c "import re; s=open('src/lib/api/__tests__/served-paths.test.ts').read(); b=s[s.index('const KNOWN_UNSERVED'):s.index('/* ---', s.index('const KNOWN_UNSERVED'))]; print('REGISTERED=%d' % len(re.findall(r'\[\s*\"/', b))); print('BOTH_DIRECTIONS=%d' % (('keeps no stale entries' in s) + ('keeps no entry for a path nothing fetches' in s)))" && python3 -c "import json; d=json.load(open('../../packages/api-client/openapi.json')); print('TWT_BACKTEST_SERVED=%d' % ('/api/v1/twt/backtest' in d['paths']))"
  EXPECT: /^REGISTERED=0$[\s\S]*^BOTH_DIRECTIONS=2$[\s\S]*^TWT_BACKTEST_SERVED=1$/m
  EVIDENCE: `REGISTERED=0`, `BOTH_DIRECTIONS=2`, `TWT_BACKTEST_SERVED=1`. Zero unserved paths in
  the whole app.

- [x] **D7: The class test fails on the real bug — all three mutants, from a clean tree.**
      Reintroduces M48's three exact edits, runs the test, restores the files from a temp copy in
      an `EXIT` trap so a failed run cannot leave the tree broken, then proves the restore. A test
      whose red has never been seen is a test nobody has verified.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && bash -c 'set -u; F=src/lib/basket/fetch.ts; C=src/lib/create/fetch.ts; T=$(mktemp -d); cp "$F" "$T/f"; cp "$C" "$T/c"; trap "cp \"$T/f\" \"$F\"; cp \"$T/c\" \"$C\"; rm -rf \"$T\"" EXIT; sed -i "" "s|readJson(\"/baskets\")|readJson(\"/discover\")|; s|readJson(\"/baskets/plan\")|readJson(\"/discover/plan\")|" "$F"; sed -i "" "s|\"/api/v1/cb/baskets\",|\"/api/v1/cb/discover\",|" "$C"; OUT=$(pnpm exec vitest run src/lib/api/__tests__/served-paths.test.ts 2>&1); echo "MUTANTS_CAUGHT=$(printf "%s" "$OUT" | grep -c "openapi.json has no such route")"; printf "%s" "$OUT" | grep -o "fetches /discover/*p*l*a*n* via wrapper:readJson()" | sort -u; printf "%s" "$OUT" | grep -o "fetches /cb/discover via literal"' ; echo "RESTORED_OK=$(grep -c 'readJson("/baskets' src/lib/basket/fetch.ts)$(grep -c '"/api/v1/cb/baskets",' src/lib/create/fetch.ts)"
  EXPECT: /^MUTANTS_CAUGHT=3$[\s\S]*fetches \/discover via wrapper:readJson\(\)[\s\S]*fetches \/discover\/plan via wrapper:readJson\(\)[\s\S]*fetches \/cb\/discover via literal[\s\S]*^RESTORED_OK=21$/m
  EVIDENCE: `MUTANTS_CAUGHT=3`, each of the three named with its file and line, and
  `RESTORED_OK=21` — two `readJson("/baskets*")` calls back, one `"/api/v1/cb/baskets"` back.

- [x] **D8: Typecheck and lint are clean on every file this leaf touched, and the suites that
      cover them are green.** Scoped to the four files on purpose: `pnpm run lint` currently also
      typechecks two **gitignored** Next build artefacts (`.next-e2e/`, `.next-gate/`) that a
      concurrent agent's e2e run left behind, and those errors are neither mine nor fixable from
      here. `tsc --noEmit` over the real `src` is clean, which is the half that is mine.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec eslint src/lib/api/__tests__/served-paths.test.ts src/lib/basket/fetch.ts src/lib/create/fetch.ts src/components/create/create-basket-form.tsx && echo "ESLINT_CLEAN=yes" && pnpm exec vitest run src/lib/api src/lib/create src/components/create src/lib/__tests__/nav.test.ts 2>&1 | tail -5
  EXPECT: /^ESLINT_CLEAN=yes$[\s\S]*Test Files {2}6 passed \(6\)[\s\S]*Tests {2}57 passed \(57\)/m
  EVIDENCE: `ESLINT_CLEAN=yes`, `Test Files 6 passed (6)`, `Tests 57 passed (57)` — including
  `nav.test.ts`, which owns the `/baskets → /discover` **UI** redirects this fix must not disturb.
  No `any`, no `// @ts-ignore`, no non-null assertion in the new test (house rule 3); the scanner
  is written against `noUncheckedIndexedAccess` with explicit `?? ""` fallbacks.

- [x] **D9: The decision is recorded where the charter says, tagged `⚠ UNREVIEWED`, with the
      evidence, the rejected options and how to reverse it.**
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && awk '/^## D-ROUTE/,0' docs/DECISIONS-MERGE.md | grep -c "⚠ UNREVIEWED\|Which side drifted\|Rejected\|To reverse\|KNOWN_UNSERVED"
  EXPECT: /^[5-9]$|^[1-9][0-9]$/m
  EVIDENCE: `docs/DECISIONS-MERGE.md` §D-ROUTE carries the heading tag, "Which side drifted, and
  the evidence", four rejected alternatives (a new route, an alias, `/explore`, fixing
  `/twt/backtest` out of lane), the register, and "To reverse".
