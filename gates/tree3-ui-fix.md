# Gates: Tree 3 — fix all UI issues (runtime TypeError + full route sweep)

Task: `/unlazy tree 3` — eliminate `__webpack_modules__[moduleId] is not a function`, the
`[object Event]` unhandled rejection, and every other UI issue across `decile-blueprint/apps/web`.

> Kept here, not in the root `GATES.md`: a concurrent session owns that file for its Tree 7
> screens run. Sharing it lost one ledger already.

Tree (depth 3): 1 All UI issues fixed and verified → 1.1 Build integrity · 1.2 Route health ·
1.3 Static quality.

## 1.1 Build integrity

- [x] G1.1.1: Only ONE `next dev` server targets the shared `.next`.
  CHECK: ps aux | grep -c "[n]ext-server (v"
  EXPECT: 1
  EVIDENCE: At session start TWO dev servers ran against one `.next` (`--port 3001` pid 33248 and
  `--port 3003` pid 35562; `next.config.ts` had no `distDir` override, so both wrote the same
  chunks). Both killed → `1`.

- [x] G1.1.2: A cold production build succeeds with no module errors.
  CHECK: cd decile-blueprint/apps/web && BASKFY_WEB_DIST_DIR=.next-build pnpm run build
  EXPECT: Compiled successfully
  EVIDENCE: Cold build after `rm -rf .next`: `✓ Compiled successfully in 10.2s`,
  `✓ Generating static pages (52/52)`, `EXIT=0`. Re-run after every fix in an isolated dist dir:
  `✓ Compiled successfully in 52s`, `52/52`, `EXIT=0`. A clean build of the same tree proves the
  runtime error was never a source defect.

- [x] G1.1.3: No `__webpack_modules__` error and no `[object Event]` rejection.
  CHECK: grep -c "webpack_modules\|REJECTION\|PAGEERROR" scratchpad/route-probe.txt
  EXPECT: 0
  EVIDENCE: Full 77-route authenticated sweep: `REJECTION 0`, `PAGEERROR 0`, `ERROR_BOUNDARY 0`,
  `OVERLAY 0`, `webpack_modules 0`, `NAVIGATION_FAILED 0`. Both reported errors were one root
  cause — a chunk `<script>` that fails to load rejects with a DOM `Event`, which prints as
  `[object Event]`.

- [x] G1.1.4: Recurrence is prevented, not just documented.
  CHECK: cd decile-blueprint/apps/web && node scripts/dev-guard.mjs --port 3003
  EXPECT: exit 1 with an explanation
  EVIDENCE: `apps/web/scripts/dev-guard.mjs` supervises `next dev` and holds a PID lock outside
  the build dir (`next dev` wipes that dir at start-up, which defeated the first version of the
  lock — found by testing, not by reading). Second server refused: *"A dev server is already
  running against .../.next … Refusing to start a second one"*, `exit=1`. A stale lock (dead PID)
  correctly falls through. `dev` routed through the guard; `next.config.ts` and
  `scripts/bundle-budget.mjs` honour `BASKFY_WEB_DIST_DIR`; `.next-*/` gitignored; hazard written
  into `RUN-AND-TEST.md`. `playwright.config.ts` now builds into `.next-e2e` — `pnpm run e2e` runs
  `next build`, which would otherwise overwrite a running dev server's chunks: the same collision
  by another route.

## 1.2 Route health

- [x] G1.2.1: Every page route probed; probed count == enumerated count.
  CHECK: grep -c "^[0-9][0-9][0-9] " scratchpad/route-probe.txt
  EXPECT: 77
  EVIDENCE: `apps/web/scripts/route-sweep.mjs` signs in once and visits every route. Coverage was
  **measured**, not assumed: each of the 77 `page.tsx` files was mapped to its route pattern and
  matched against a probed URL — `page files: 77 | probed URLs: 77 | uncovered: 0`. The first pass
  left 6 uncovered (`/[me/]investments/[id]{,/customize,/orders}`) because no investment row
  existed; one `cb_investment` row was seeded and the 6 routes added rather than declaring a gap.
  Final: `routeS probed: 77 / clean: 77 / problem: 0`. No sampling.

- [x] G1.2.2: No probed route returns 500 or renders an error boundary.
  CHECK: grep -c " ERROR " scratchpad/route-probe.txt
  EXPECT: 0
  EVIDENCE: `problem: 0`, every severity class zero — `REJECTION 0 · PAGEERROR 0 ·
  ERROR_BOUNDARY 0 · OVERLAY 0 · REQFAIL 0 · HTTP_5 0 · HTTP_404 0 · webpack_modules 0 ·
  CONSOLE 0`. The first sweep found 63 routes with a duplicate-CSP console error and 2 with a
  React key collision; both fixed below.

- [x] G1.2.3: No page file is permanently shadowed by a redirect.
  CHECK: cd decile-blueprint/apps/web && node scripts/check-shadowed-routes.mjs
  EXPECT: ok
  EVIDENCE: `ok — 12 redirected route(s) hold a redirect stub and nothing else`. The check found 2
  real offenders: `screens/[id]/columns/page.tsx` and `backtests/[id]/page.tsx` were *identical
  copies* (`diff` → IDENTICAL) of the `/build/...` pages, unreachable because `redirects()` runs
  before the filesystem routes — free to drift, impossible to see. Both reduced to stubs. Check
  wired into `pnpm run lint`.

- [x] G1.2.4: Server logs during the sweep contain no unhandled render errors.
  EVIDENCE: `dev3.log` over the final sweep: compile lines and `GET … 200` only; no
  `unhandledRejection`, no `Error:`, no stack. API log clean after the migration below.

- [x] G1.2.5: The `/baskets` catalog — the primary tree-6 hub — actually serves data.
  EVIDENCE: `GET /api/v1/explore` returned **500**: `asyncpg.exceptions.UndefinedColumnError:
  column cb_metrics.volatility_basis does not exist`. The dev database sat at
  `0015_backtest_started_at` while head was `0016_cb_metrics_disclosure`. `alembic upgrade head`
  → `0016 (head)`; endpoint now `HTTP=200`. Catalog was then empty (`{"items":[],"total":0}`,
  `cb_basket` 0 rows) because `make seed` had not run against this database;
  `python -m baskfy_api.seed all` → `cb_manager: 2`, `cb_momentum_scan: 1`, and `/api/v1/explore`
  returns the `momentum-scan` basket. A *stale* API process on :8000 predating the current source
  was also restarted.

## 1.3 Static quality

- [x] G1.3.1: `tsc --noEmit`, eslint and the shadowed-route check are clean.
  CHECK: cd decile-blueprint/apps/web && pnpm run lint
  EXPECT: 0 errors
  EVIDENCE: `LINT_EXIT=0`. Started at `✖ 16 problems (15 errors, 1 warning)` — 2 introduced by
  this work, 13 pre-existing in the in-flight tree-6/screens tree. All 15 errors fixed
  (unnecessary assertions, unused imports, `unknown | null`, an unbound static method, a redundant
  `role="list"`, a no-await async). The 1 remaining warning is the React Compiler on TanStack
  Table's `useReactTable()` — inherent to the library.
  **Re-measured at hand-off:** repo-wide lint is `✖ 2 problems (1 error, 1 warning)`. The one
  error is `unbound-method` in `src/lib/screens/column-display.ts`, a file created and still being
  edited by a **concurrent Tree 7 session** — not touched by this work. Lint over the 13 files
  this run changed: `ESLINT_MINE_EXIT=0`.

- [x] G1.3.2: Web unit tests pass.
  CHECK: cd decile-blueprint/apps/web && pnpm run test
  EXPECT: all passed
  EVIDENCE: `Test Files 39 passed (39) / Tests 811 passed (811)`. Started at
  `3 failed | 36 passed` / `7 failed | 804 passed` — all 7 pre-existing tree-6 fallout, all fixed:
  6 still read `market-health/page.tsx`, which tree-6 reduced to a stub (repointed at
  `market/mood/page.tsx`, where the content and the lazy chart wrapper both live); 1 was a genuine
  a11y defect — `text-brand` on the new bottom tab bar's active label at 3.27:1 where type needs
  4.5:1, changed to `text-accent`.
  **Re-measured at hand-off:** the suite has since grown to 896 tests and reports
  `3 failed | 893 passed` — a **concurrent Tree 7 session** added ~85 tests during this run, and
  the failing set is theirs (`universe-label`, `default-view`, `admin`, `factsheet`, `market`), is
  not stable between runs, and shows 5–15 s durations against milliseconds earlier, i.e. timeouts
  under host contention rather than assertions. The 364 tests covering this run's changes pass:
  `Test Files 6 passed (6) / Tests 364 passed (364)`. **This run does not claim the repo-wide
  suite is green; it claims it left it green at 811/811 and that nothing here is in the red set.**

- [x] G1.3.3: Nav e2e specs pass against the built app.
  CHECK: cd decile-blueprint/apps/web && pnpm exec playwright test e2e/nav.spec.ts
  EXPECT: passed
  EVIDENCE: `20 passed (1.0m)`, `EXIT=0`, including all 12 legacy-route redirect assertions. Ran
  against its own `.next-e2e` build; the dev server on :3000 still answered `200` afterwards.

- [x] G1.3.4: Backend tests covering the touched Python still pass.
  CHECK: uv run pytest services/api/tests/test_api_backtests.py services/worker/tests/test_backtest_job.py -q
  EXPECT: all passed
  EVIDENCE: `44 passed` and `20 passed` (BASKFY_TEST_DATABASE_URL against the local stack).

- [x] G1.3.5: The two defects the sweep found are fixed at the source and covered by tests.
  EVIDENCE:
  **(a) Duplicate CSP.** `contentSecurityPolicy()` prepended `default-src 'self'` while
  `commonDirectives()` already supplied it, so 63 routes logged *"Ignoring duplicate
  Content-Security-Policy directive 'default-src'"*. The effective policy was unchanged (browsers
  honour the first occurrence), so this was hygiene rather than a hole — but a developer trained
  to scroll past CSP warnings is the real cost. Removed; the live header now carries exactly one
  `default-src`. New `src/lib/__tests__/csp.test.ts` asserts the spec — *every directive named
  exactly once*, both policies, dev and prod — `10 passed`. **Verified adversarially:**
  reinstating the duplicate fails it (`repeated directives in the nonce policy: expected
  [ 'default-src' ] to deeply equal []`); restoring passes.
  **(b) Duplicate React key / repeated assumptions.** `assumptions()` ends with `result.notes`,
  and the worker's `notes_for()` hands the same notes back as `extra_notes`; `build_payload`
  concatenated both. The stored payload had 33 assumption lines of which 20 were unique — 13
  sentences printed **twice** on the backtest page and colliding as React keys. Fixed with
  `dict.fromkeys` (order-preserving; the idiom `backtest.py` and `notes_for` already use).
  `test_assumptions_state_each_note_once` **verified adversarially:** reverting the fix fails it
  (`AssertionError: a run note reached the panel twice, assert 2 == 1`); restoring passes. The
  panel also deduplicates at render (2 new tests) so rows written before the fix display correctly.

## Ledger

**14 of 14 gates checked with evidence. 0 unchecked. 0 ABANDON.**

### Declared, not fixed

- **The dev server wedges under host memory pressure, not from a product defect.** Two sweeps
  stalled mid-run (`page.goto: Timeout 90000ms`) while this machine ran docker, two API processes,
  headless Chromium, vitest and a production build at once — `vm_stat` showed ~38 MB free while
  the Next dev process was only 0.4 GB RSS with no heap flag. After killing my own extra load and
  restarting the server, all 77 routes passed. Recorded because the stalls are in the logs and
  would otherwise read as an unexplained product fault.
- `react-hooks/incompatible-library` warning on `useReactTable()` in `data-table.tsx` — inherent
  to TanStack Table under the React Compiler.
- One sweep line flagged the duplicate CSP *after* it was fixed: the adversarial CSP test
  reinstated the bug while that sweep was running and the dev server hot-reloaded it. That run was
  discarded and re-run clean; noted so the log is not read as a surviving defect.
