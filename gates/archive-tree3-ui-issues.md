# Gates: Tree 3 — fix all UI issues (runtime TypeError + full route sweep)

> **Superseded by `gates/tree3-ui-fix.md`** — the completed 14-gate ledger for this run.
> Kept because a concurrent Tree 7 session archived it here when it took over the root `GATES.md`.


Task: `/unlazy tree 3` — eliminate `__webpack_modules__[moduleId] is not a function`, the
`[object Event]` unhandled rejection, and every other UI issue across `decile-blueprint/apps/web`.

Tree (depth 3):
- 1 All UI issues fixed and verified
  - 1.1 Build integrity — the two runtime errors
  - 1.2 Route health
  - 1.3 Static quality

## 1.1 Build integrity

- [x] G1.1.1: Only ONE `next dev` server targets the shared `.next`; the concurrent-server
      corruption is gone.
  CHECK: ps aux | grep -c "[n]ext-server (v"
  EXPECT: 1
  EVIDENCE: At session start TWO dev servers ran against one `.next` (`pnpm ... run dev --port 3001`
  pid 33248 and `--port 3003` pid 35562; `next.config.ts` had no `distDir` override, so both wrote
  the same chunks). Both killed. `ps aux | grep -c '[n]ext-server (v'` → `1`.

- [x] G1.1.2: A cold production build succeeds with no module errors.
  CHECK: cd decile-blueprint/apps/web && BASKFY_WEB_DIST_DIR=.next-build pnpm run build
  EXPECT: Compiled successfully
  EVIDENCE: Cold build after `rm -rf .next`: `✓ Compiled successfully in 10.2s`,
  `✓ Generating static pages (52/52)`, `EXIT=0`. Re-run after every fix in an isolated dist dir:
  `✓ Compiled successfully in 52s`, `52/52`, `EXIT=0`. A clean build of the same tree proves the
  runtime error was never a source defect.

- [x] G1.1.3: No `__webpack_modules__` error and no `[object Event]` rejection when the server
      renders the app.
  CHECK: grep -c "webpack_modules\|REJECTION\|PAGEERROR" scratchpad/route-probe.txt
  EXPECT: 0
  EVIDENCE: Full 71-route authenticated sweep: `REJECTION 0`, `PAGEERROR 0`, `ERROR_BOUNDARY 0`,
  `OVERLAY 0`, `webpack_modules 0`, `NAVIGATION_FAILED 0`. Both reported errors were the same root
  cause — a chunk `<script>` that fails to load rejects with a DOM `Event`, which is what prints
  as `[object Event]`.

- [x] G1.1.4: Recurrence is prevented, not just documented.
  CHECK: cd decile-blueprint/apps/web && node scripts/dev-guard.mjs --port 3003
  EXPECT: exit 1 with an explanation
  EVIDENCE: `apps/web/scripts/dev-guard.mjs` supervises `next dev` and holds a PID lock outside
  the build dir (`next dev` wipes the build dir at start-up, which defeated a lock kept inside it).
  Second server refused: *"A dev server is already running against .../.next … Refusing to start a
  second one"*, `exit=1`. Stale lock (dead PID) correctly falls through and starts. `dev` script
  routed through the guard; `next.config.ts` + `scripts/bundle-budget.mjs` honour
  `BASKFY_WEB_DIST_DIR`; `.next-*/` gitignored; hazard written into `RUN-AND-TEST.md`.
  `playwright.config.ts` now builds into `.next-e2e` — `pnpm run e2e` used to `next build` over a
  running dev server's `.next`, the same collision by another route.

## 1.2 Route health

- [x] G1.2.1: Every page route probed against a running server; probed count == enumerated count.
  CHECK: grep -c "^[0-9][0-9][0-9] " scratchpad/route-probe.txt
  EXPECT: 71
  EVIDENCE: `apps/web/scripts/route-sweep.mjs` signs in once and visits all 71 routes (78 `page.tsx`
  files minus 7 that are dynamic-segment duplicates already covered by a concrete id). Output:
  `routeS probed: 71 / clean: 71 / problem: 0`. No sampling.

- [x] G1.2.2: No probed route returns 500 or renders an error boundary.
  CHECK: grep -c " ERROR " scratchpad/route-probe.txt
  EXPECT: 0
  EVIDENCE: Final sweep `problem: 0`; `HTTP_5 0`, `HTTP_404 0`, `REQFAIL 0`. First sweep found 63
  routes with a duplicate-CSP console error and 2 with a React key collision; both fixed below.

- [x] G1.2.3: No page file is permanently shadowed by a redirect.
  CHECK: cd decile-blueprint/apps/web && node scripts/check-shadowed-routes.mjs
  EXPECT: ok
  EVIDENCE: `ok — 12 redirected route(s) hold a redirect stub and nothing else`. The check found 2
  real offenders: `screens/[id]/columns/page.tsx` and `backtests/[id]/page.tsx` were *identical
  copies* (`diff` → IDENTICAL) of the `/build/...` pages, unreachable because `redirects()` runs
  before the filesystem routes — free to drift, impossible to see. Both reduced to redirect stubs.
  Check wired into `pnpm run lint`.

- [x] G1.2.4: Server logs during the sweep contain no unhandled render errors.
  EVIDENCE: `scratchpad/dev1.log` over both sweeps: compile lines and `GET … 200` only; no
  `unhandledRejection`, no `Error:`, no stack. API log `scratchpad/api8000.log` clean after the
  migration below.

- [x] G1.2.5: The `/baskets` catalog — the primary tree-6 hub — actually serves data.
  EVIDENCE: `GET /api/v1/explore` returned **500**: `asyncpg.exceptions.UndefinedColumnError:
  column cb_metrics.volatility_basis does not exist`. The dev database was at `0015_backtest_started_at`
  while head was `0016_cb_metrics_disclosure`. `alembic upgrade head` → `0016 (head)`; endpoint now
  `HTTP=200`. Catalog was then empty (`{"items":[],"total":0}`, `cb_basket` 0 rows) because
  `make seed` had not been run against this database; `python -m baskfy_api.seed all` →
  `cb_manager: 2`, `cb_momentum_scan: 1`, and `/api/v1/explore` returns the `momentum-scan` basket.
  (Also restarted a *stale* API process on :8000 that predated the current source.)

## 1.3 Static quality

- [x] G1.3.1: `tsc --noEmit`, eslint and the shadowed-route check are clean.
  CHECK: cd decile-blueprint/apps/web && pnpm run lint
  EXPECT: 0 errors
  EVIDENCE: `LINT_EXIT=0`. Started at `✖ 16 problems (15 errors, 1 warning)` — 2 introduced by this
  work, 13 pre-existing in the in-flight tree-6/screens tree. All 15 errors fixed (unnecessary
  assertions, unused imports, `unknown | null`, an unbound static method, a redundant `role="list"`,
  a no-await async). The 1 remaining warning is React Compiler on TanStack Table's
  `useReactTable()` — inherent to the library, not actionable.

- [x] G1.3.2: Web unit tests pass.
  CHECK: cd decile-blueprint/apps/web && pnpm run test
  EXPECT: all passed
  EVIDENCE: `Test Files 39 passed (39) / Tests 811 passed (811)`. Started at
  `3 failed | 36 passed` / `7 failed | 804 passed` — all 7 pre-existing tree-6 fallout, all fixed
  (6 tests still read `market-health/page.tsx`, which tree-6 reduced to a stub — repointed at
  `market/mood/page.tsx`; 1 was a genuine a11y defect, `text-brand` on the new bottom tab bar's
  active label at 3.27:1 where type needs 4.5:1, changed to `text-accent`).

- [x] G1.3.3: Nav e2e specs pass against the built app.
  CHECK: cd decile-blueprint/apps/web && pnpm exec playwright test e2e/nav.spec.ts
  EXPECT: passed
  EVIDENCE: `20 passed (1.0m)`, `EXIT=0`, including all 12 legacy-route redirect assertions. Ran
  against its own `.next-e2e` build; the dev server on :3000 was still `200` afterwards.

- [x] G1.3.4: The two defects the sweep found are fixed at the source and covered by tests.
  EVIDENCE: Two defects, both fixed at source and both covered by an adversarial test — re-verified 25 Aug 2026 by `tools/tree3b/verify-sweep-claims.sh` (csp.test.ts 10 passed; exactly 1 `default-src` directive in middleware.ts; `dict.fromkeys` present; `test_assumptions_state_each_note_once` 1 passed). Detail:
  **(a) Duplicate CSP.** `contentSecurityPolicy()` prepended `default-src 'self'` while
  `commonDirectives()` already supplied it, so 63 routes logged *"Ignoring duplicate
  Content-Security-Policy directive 'default-src'"*. Effective policy was unchanged (browsers honour
  the first), so this was hygiene, not a hole — but a developer trained to scroll past CSP warnings
  is the actual cost. Removed. New `src/lib/__tests__/csp.test.ts` asserts the spec — *every
  directive named exactly once*, both policies, dev and prod — `10 passed`.
  **(b) Duplicate React key / repeated assumptions.** `assumptions()` ends with `result.notes`, and
  the worker's `notes_for()` hands the same notes back as `extra_notes`; `build_payload` concatenated
  both. Stored payload had 33 assumption lines, 20 unique — 13 sentences printed **twice** on the
  backtest page and colliding as React keys. Fixed with `dict.fromkeys` (order-preserving, the house
  idiom). `test_assumptions_state_each_note_once` **verified adversarially**: reverting the fix
  fails it (`AssertionError: a run note reached the panel twice, assert 2 == 1`), restoring passes.
  Panel also deduplicates at render (2 new tests) so rows written before the fix display correctly.

## Ledger

12 of 12 gates checked with evidence. 0 ABANDON.
