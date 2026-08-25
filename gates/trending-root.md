# Gates: Trending and ranked lists + collections seed (tree 3)

Scope: the deferred half of SC9. A pure ranking domain, a persisted EOD snapshot written by a
Celery task on Beat, a read API, a rule-backed collections seed, and the two web surfaces that
render them. Track C held: no order-shaped route is added.

The design decision this tree turns on, taken from `docs/smallcase/01-requirements.md` §A.14
("in single-tenant mode most rankings degenerate — compute what is computable, stub the rest
honestly"): a ranked list states the population it was computed over, and **withholds itself with
a machine-readable reason** rather than publishing a ranking drawn from too few baskets or too
few people. With one basket and one investor in the live database today, every list correctly
withholds — and the same code ranks properly against a populated fixture, which is what the
tests assert.

Working root for every CHECK: `/Users/maulikdave/Documents/projects/baskfy`.
Python CHECKs run from `decile-blueprint/` under `uv run`.

---

## A — the ranking domain (pure)

- [ ] G1: `baskfy_core.curated_trending` defines every list once, with its metric, direction and
      whether it depends on a population. Nine lists, no duplicates, no list without a definition.
  CHECK: cd decile-blueprint && uv run python -c "from baskfy_core.curated_trending import TRENDING_LISTS, TrendingListKey; ks=[d.key for d in TRENDING_LISTS]; assert len(ks)==len(set(ks)); print(f'LISTS={len(ks)} POP={sum(1 for d in TRENDING_LISTS if d.population_based)}')"
  EXPECT: LISTS=9 POP=3
  EVIDENCE: pending

- [ ] G2: House rule 1 holds — the module touches nothing. No database, network, disk or clock
      import anywhere in it.
  CHECK: cd decile-blueprint && ! rg -n "^\s*(import|from)\s+(sqlalchemy|httpx|requests|asyncpg|os|pathlib|datetime\s+import\s+datetime\b)" packages/core/src/baskfy_core/curated_trending.py && echo NO_IO_IMPORTS
  EXPECT: NO_IO_IMPORTS
  EVIDENCE: pending

- [ ] G3: Ranking is deterministic and honest about absent data: ties break on slug, NULL metrics
      are excluded rather than sorted last, and direction is per-list.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_trending.py -q -p no:randomly 2>&1 | tail -3
  EXPECT: /\d+ passed/
  EVIDENCE: pending

- [ ] G4: The floors are enforced in the pure layer: a list below `MIN_ENTRIES` baskets or (when
      population-based) below `MIN_POPULATION` distinct users is withheld with a named reason,
      never published thin.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_trending.py -q -p no:randomly -k "withheld or floor or population" 2>&1 | tail -3
  EXPECT: /\d+ passed/
  EVIDENCE: pending

## B — persistence, job, API, seed

- [ ] G5: Migration 0020 creates the snapshot tables and genuinely round-trips
      (upgrade → downgrade → upgrade against the live database).
  CHECK: cd decile-blueprint && uv run alembic upgrade head >/dev/null 2>&1 && uv run alembic downgrade -1 >/dev/null 2>&1 && uv run alembic upgrade head >/dev/null 2>&1 && uv run alembic current 2>/dev/null | tail -1
  EXPECT: 0020
  EVIDENCE: pending

- [ ] G6: The task is registered on the app and on Beat, on the compute queue like its siblings.
  CHECK: cd decile-blueprint && uv run python -c "from baskfy_worker.celery_app import app, BEAT_SCHEDULE; k=[k for k in BEAT_SCHEDULE if 'trending' in k]; print('BEAT='+','.join(k)); print('TASK='+('baskfy.cb.compute_trending' in app.tasks and 'yes' or 'no'))"
  EXPECT: TASK=yes
  EVIDENCE: pending

- [ ] G7: `GET /api/v1/cb/trending` answers at the HTTP boundary (not as a bare coroutine),
      requires authentication, and returns every list with its population and withheld reason.
  CHECK: cd decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest services/api/tests/test_curated_trending_http.py -q -p no:randomly 2>&1 | tail -3
  EXPECT: /\d+ passed/
  EVIDENCE: pending

- [ ] G8: The collections seed is rule-backed and idempotent — running it twice leaves the same
      row count and the same membership.
  CHECK: cd decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest services/api/tests/test_curated_collections_seed.py -q -p no:randomly 2>&1 | tail -3
  EXPECT: /\d+ passed/
  EVIDENCE: pending

- [ ] G9: The job actually ran against the live database and the API served its output — proved
      end to end, not by unit test.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && bash gates/trending-e2e.sh 2>&1 | tail -6
  EXPECT: E2E_OK
  EVIDENCE: pending

## C — web

- [ ] G10: `/collections/[slug]` exists and the trending module renders on the catalog page,
      including the withheld state with its reason in words a person can read.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/explore/__tests__/trending.test.tsx src/lib/explore/__tests__/trending.test.ts 2>&1 | tail -4
  EXPECT: /Tests {2}\d+ passed/
  EVIDENCE: pending

- [ ] G11: The web surfaces carry the price-return caveat that every returns-based number on this
      product owes the reader (A6).
  EVIDENCE: pending

## Cross-cutting

- [ ] G12: Track C held — this tree adds no order-shaped route.
  CHECK: cd decile-blueprint && rg -n "place_order|/execute|OrderGateway" services/api/src/baskfy_api/routers/curated_trending.py packages/core/src/baskfy_core/curated_trending.py apps/web/src/components/explore 2>/dev/null | wc -l | awk '{print "ORDER_HITS="$1}'
  EXPECT: ORDER_HITS=0
  EVIDENCE: pending

- [ ] G13: Every file this tree owns is ruff-clean, ruff-format-clean and mypy-clean; the web
      files are eslint-clean and tsc-clean.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && bash gates/trending-lint.sh 2>&1 | tail -6
  EXPECT: LINT_OK
  EVIDENCE: pending

- [ ] G14: Nothing this tree did broke a sibling: the curated core and API suites still pass at
      or above the count measured before the tree started.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests -q -p no:randomly -k "curated or scan_projection" 2>&1 | tail -3
  EXPECT: /\d+ passed/
  EVIDENCE: pending

- [ ] G15: The judgement calls are recorded where this repo records them, tagged UNREVIEWED, and
      the status page says what is now done and what is not.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && rg -c "Trending" docs/smallcase/DECISIONS-SC.md docs/smallcase/STATUS.md | tr '\n' ' '
  EXPECT: /DECISIONS-SC\.md:[1-9]/
  EVIDENCE: pending

- [ ] G16: Report numbers are re-measured at report time, not written from memory.
  EVIDENCE: pending

---

## Outcome: ABANDONED at G4 — collision, not difficulty

A second session was building this same feature concurrently and overwrote
`packages/core/src/baskfy_core/curated_trending.py` while this tree's tests were being run
against it. Their module is a different design under the same module path: uppercase keys
(`TOP_1M`, `TOP_CAGR_5Y`), `TOO_FEW_BASKETS` / `TOO_FEW_PEOPLE` reasons, `rank_lists` instead of
`rank_all`, a `TrendingPopulation` record, and a `ranks_by` label per list.

Their design is anchored better than mine on one point that matters. It cites
`docs/smallcase/06-module-plan.md` §SC9, which I had not read — I built from
`01-requirements.md` §A.14 alone. SC9 requires "each list labeled with what it actually ranks",
which is exactly what their `ranks_by` field is and which my `blurb` was not written to satisfy.
Theirs also produces `metric_display`, honouring house rule 8; mine left rounding to the caller.

Continuing would have meant two agents writing one module, one migration number and one router.
This repo has already paid for that once — `docs/smallcase/STATUS.md`, "A tree that is not the
one that will ship": two sessions each reported the other's tree broken while each was reading a
real tree accurately, just not the one the other stood in. Migration `0020` was taken by the
managers tree at 19:25 while this tree was still planning to use it.

ABANDON: G1 curated_trending.py is owned by a concurrent session; its module defines 9 lists under different keys
ABANDON: G2 same file, not this tree's to assert over
ABANDON: G3 test file removed rather than left red against the other session's module (preserved in scratchpad)
ABANDON: G4 floors exist in their module as TOO_FEW_BASKETS / TOO_FEW_PEOPLE; not this tree's to gate
ABANDON: G5 migration 0020 was taken by 0020_manager_identity while this tree was planning
ABANDON: G6 job not written — depends on the abandoned domain module
ABANDON: G7 API not written — depends on the abandoned domain module
ABANDON: G8 collections seed not written — gates/collections.md indicates another session owns it
ABANDON: G9 no end-to-end run to prove; nothing of this tree's reached the database
ABANDON: G10 web surface not written
ABANDON: G11 web surface not written
ABANDON: G12 no route added by this tree, so nothing to hold — vacuous rather than met
ABANDON: G13 no owned files remain in the tree
ABANDON: G14 measured anyway, and green — see evidence below
ABANDON: G15 decisions not recorded; the design they document is not the one that shipped
ABANDON: G16 the report states measurements, not gate completions

## What was actually established, measured not remembered

- Sibling suite unharmed: `packages/core/tests -k "curated or scan_projection"` was
  **163 passed** before this tree started and **164 passed** after its only artifact was removed.
  No failures introduced, no test left red.
- `test_no_escape_hatches.py` **does** scan `packages/core/tests`, not only `src`. Proved by
  running it against a file carrying five `# type: ignore[arg-type]` comments: `1 failed`, naming
  the first offender by line. After the ignores were replaced with `dataclasses.replace`, the same
  check returned `8 passed`. Worth knowing: a test-directory ignore fails the build exactly as a
  source one does.
- SC9's acceptance criterion may prohibit what **both** implementations built — see the handover
  note below.

## Handover: one finding neither implementation resolves

`docs/smallcase/06-module-plan.md` §SC9 reads, in full:

> Trending jobs: computable rankings only (top by 1M/1Y return, recently rebalanced,
> budget-friendly by min-amount, most-watched degenerates to watchlist recency) — each list
> labeled with what it actually ranks; no fake "most invested".

Two clauses cut against both designs:

1. **"most-watched degenerates to watchlist recency"** asks for the list to be *recast* into
   something computable — ordered by when each basket was last watchlisted — not withheld behind
   a population floor. Both implementations withhold it instead.
2. **"computable rankings only … no fake 'most invested'"** reads as a scope statement: a
   `MOST_INVESTED` list should not exist at all. Both implementations ship one that withholds.
   Whether a withheld list counts as "fake" is a judgement call, and it is the owner's to make
   and record — it is currently made implicitly, by both trees, in the same direction, with no
   entry in `DECISIONS-SC.md`.

Owner: whoever holds the trending module. This is a spec-versus-build question, not a defect.
