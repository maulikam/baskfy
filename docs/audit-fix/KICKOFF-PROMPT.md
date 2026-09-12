# AF run — fix AUDIT-2026-09-12.md, fast

Paste everything below the line into Claude Code at the repo root, branch `developer`.

---

You are the orchestrator of the AF run. Goal: every item in `AUDIT-2026-09-12.md` §0–§5 and §7
fixed, tested, merged and **deployed to the box**, in two waves, with the minimum of words. Read the
audit and `CLAUDE.md` once. Then spawn subagents and merge. You write no code yourself.

## Token discipline (applies to you and every subagent)

- Write code, tests and commits. Do not write reports, summaries, runbooks, briefs, narrated
  progress, or restatements of the audit. The audit already says what and where; the diff says
  how.
- The only prose artefacts of this run: `docs/audit-fix/STATUS.md` (one table, one row per audit
  item: item · lane · commit · state), one-line `⚠ UNREVIEWED` entries in `docs/DECISIONS-MERGE.md`
  for real judgement calls only, and one-line `NEEDS-MAULIK.md` entries for things only Maulik
  holds (a secret to rotate, a product decision). Nothing else. No `AF-FINAL-REPORT.md`, no
  per-lane gate files, no HANDOFFS essays.
- Commit messages: `AF<lane>: <one line>`. No paragraphs.
- Subagent prompts: the rules block below + the lane's ownership list + "fix these audit items:"
  followed by the item ids. Do not paste the audit into the prompt; they read the file.
- A subagent runs only the tests for files it touched (`pytest <path>`, `vitest run <path>`,
  `tsc --noEmit`). Full suites and `make lint` run once per wave, by you, at merge. Never two
  heavy suites at once (`REMAINING.md` §1): db tests, web vitest, mypy — one at a time.
- If a subagent is stuck on one item for more than two attempts, it records the item as
  `deferred: <reason>` in STATUS.md and moves to the next. It does not explain at length.

## Rules block (paste verbatim into every subagent prompt)

```
Read CLAUDE.md first; its laws, non-negotiables and house rules bind you. DRY_RUN=true; never a
live order. No `# type: ignore`, no `any`, no swallowed exceptions; money is Decimal/numeric.
Fix only your items; touch only the files you own; if you need another file, add one line to
docs/audit-fix/STATUS.md ("needs <file>: <what>") and continue.
Every fix ships with a test that fails on the old code. Never weaken a test; if a test pins the
bug (csp.test.ts:54, test_api_auth.py:579) rewrite it to the spec.
A doc that disagrees with code: `git log -S` first; a decision beats a doc; fix the doc in one line.
Data lives on the AWS box only. Measure with `tools/deploy/box-sql.sh "<select>"`
(BOX_SQL_LINES=50). Never pull a dump, extract, or snapshot to the laptop; never push data up.
The only local DB is the throwaway test Postgres (`make up`, BASKFY_TEST_DATABASE_URL,
`make test-db`) with synthetic fixtures. Never pass a secret through box.sh.
Run only the tests for files you touched. Commit per item: `AF<lane>: <one line>`. Update your
rows in docs/audit-fix/STATUS.md (item · commit · state) and stop. No report.
```

## Procedure

1. `aws sts get-caller-identity` (if dead: `aws sso login --sso-session baskfy` — you, not
   Maulik) and `bash tools/deploy/verify-stack.sh`. Record the box's tag from
   `tools/deploy/verify-pc-deploy.sh` at the top of `docs/audit-fix/STATUS.md` as the rollback
   tag. Seed STATUS.md with the item table (ids below). Commit `AF0: seeded`.
2. Wave 1 — spawn all seven lanes at once, each in `git worktree add ../baskfy-<lane> -b af/<lane> developer`:
   **A B C D E F G**. Wait for all.
3. Merge A→B→C→D→E→F→G into `developer` (rebase each first). Then, one at a time: `make test-db`,
   `make test`, web `vitest run`, `make lint`, desk suite. Fix breakage on `developer` directly —
   no new lane. Then `bash tools/deploy/ship.sh`. It refuses during 09:15–15:30 IST weekdays and
   18:40–21:15 IST Mon–Fri; wait, never bypass. If its verify step fails: redeploy the rollback
   tag (`push-images.sh` + `deploy-swing.sh` at that tag), fix on `developer`, ship again.
4. `bash ops/af/smoke.sh` (lane B writes it) against staging. All lines must pass before wave 2.
5. Wave 2 — spawn in parallel: **H I** (code) and **D2** (the box job, same subagent as D,
   resumed). Wait, merge H→I, run the suites once, ship, smoke.
6. Fill the last column of STATUS.md, append `## Status (AF run)` to `AUDIT-2026-09-12.md` as
   one line per §0 item, commit `AF: done`. Stop.

## Lanes and item ids

Item ids refer to the audit: `0.n` = §0 table row, `1.<row>` = §1 bullets in order (P0 rows
1–9, P2 rows 10–24), `2.<row>` = §2 table rows top to bottom (1–15), `3.<n>` = §3 bullets in
order (1–13: 1 = the #5/#6 line, 11 = Worker, 12 = three clocks, 13 = valued at cost), `4.<n>` =
§4 bullets (1–10; the "API robustness" paragraph is 4.11–4.16 in the order written), `5.<n>` =
§5 items, `7` = every §7 nit. Every id below appears in exactly one lane; STATUS.md has one row
per id.

### A — Auth, sessions, tenancy (API + web)
Owns: `services/api/src/baskfy_api/{auth,settings,ratelimit,app,csrf}.py`,
`services/api/src/baskfy_api/routers/{desk,baskets,kite,auth,brokers,swing,webhook_endpoints}.py`,
worker webhook delivery guard, `apps/web/src/lib/auth/**`, `apps/web/src/app/logout/**`,
`apps/web/src/app/api/v1/brokers/callback/**`, `apps/web/next.config.ts` (headers), `NEEDS-MAULIK.md` §22.
Items: 0.1, 0.8, 2.1, 2.2, 2.4, 2.5, 2.6, 2.10–2.15, 4.11 (the `brokers.py` calls → `anyio.to_thread.run_sync`), 4.16 (add a generic bad-request problem type, replace the 49 misuses).
Decisions fixed in advance (no deliberation): kite route → `AuthenticatedDep` + `PUBLISHED` +
published version; `/me/restore` → signed code from the deletion e-mail; sole-tenant check on
broker callback/sync + production refuses empty `BASKFY_LOGIN_ALLOWLIST`; SSRF → resolve and
block private/link-local/loopback at create and delivery; epoch claim minted in `jwt.ts`,
refused API-side and in the Auth.js re-mint path; `/logout` → POST; HSTS preload prod-only;
drop `?now=`; add `X-CSRF-Token` to CORS.

### B — CSP, config, staging smoke
Owns: `apps/web/src/middleware.ts`, `apps/web/src/lib/api/config.ts`, `apps/web/src/lib/site.ts`,
`apps/web/src/components/cb/plan-handoff-panel.tsx` (env only), `apps/web/src/lib/__tests__/{csp,api-origin-split}.test.ts`,
`.env.example`, `.env.staging.example`, `apps/web/.gitignore`, `ops/af/smoke.sh` (new).
Items: 0.2, 2.3, 2.7, 2.8, 2.9, 4.6.
Fixed: `form-action 'self' https://kite.zerodha.com`; Razorpay `frame-src`/`connect-src`/`img-src`;
one origin resolver, delete `NEXT_PUBLIC_API_ORIGIN`, no localhost fallback in production;
`NEXT_PUBLIC_DESK_URL`/`SITE_URL`/`REVALIDATE_SECRET` validated at startup; `.env*` ignored,
`git ls-files` confirms `.env.local` untracked (rotation question → one NEEDS-MAULIK line).
`ops/af/smoke.sh`: curl checks against `https://staging.baskfy.com` — anonymous
`/api/v1/{desk/performance,baskets,baskets/plan,explore/broad-market-sharpe/kite}` → 401; CSP
header contains the Kite origin and `frame-src`; `/api/v1/meta/status` `degraded` false;
`/this-does-not-exist` body contains the nav; the constituents API `weight_pct` = `5.00`. Exit
non-zero on any miss. Gate password from `ops/baskfy-staging-gate-password.txt`, read into a
variable, never echoed.

### C — Portfolio numbers, clock, sync status
Owns: `services/api/src/baskfy_api/routers/{portfolio_overview,curated_investments,curated_costs,curated_drift,portfolios,curated_sip,curated_create,curated_from_screen}.py`,
`services/api/src/baskfy_api/{live_prices,vbt,invoices,swing_scan,vbt_scan,twt_scan}.py`,
`routers/kite.py:194` and `routers/explore.py:335` (date only — one STATUS line to A),
`apps/web/src/components/portfolio/live-refresh.tsx`, `apps/web/src/components/shell/freshness-pill.tsx`,
`apps/web/src/components/portfolio/command-center-screen.tsx`, `apps/web/src/app/(app)/portfolio/{holdings,activity}/**`,
`CLAUDE.md` (clock table only).
Items: 0.4, 0.9, 1.3, 1.6, 3.12, 3.13, 4.1, 4.11 (`live_prices.py` only), 4.12, 4.13, 4.14, 4.15.
Fixed: TWR/drawdown/peak return `None` until ≥2 real valuations; `previous` = stored recency-1
close when overlaid; one `today_ist()`; curated marks → live overlay when available else
`marked_at_cost: true` shown in UI; holdings page lists every holding grouped by portfolio; one
sync-status field for all three surfaces; clock decision = "close, plus live overlay when a Kite
session exists" — pill, copy and CLAUDE.md say exactly that; `router.refresh()` only during the
session and only when the overlay is on; Kite I/O in `live_prices.py` via `to_thread`; scan
tasks published after commit like `backtests.py:397-404`; `FOR UPDATE` on share allocation;
drift no longer continues on an aborted session; N+1 collapsed.

### D — Data plant (code now, box job in wave 2)
Owns: `packages/providers/**`, `services/worker/src/**/{bars,adjustments,catch_up,calendar,orchestrator,celery_app}.py`,
listings query (`market_data.py` / `routers/market_data.py`), alembic migrations you add, `ops/af/data/**` (new).
Items: 0.5, 0.6, 1.4, 1.8, 3.1, 3.2, 3.3, 3.11.
Fixed: `source='kite_adjusted'` for deep-history rows (migration), `adjust_bars` covers nightly
`kite`; on-conflict skip OHL overwrite when `adj_factor ≠ 1` and add `open_raw/high_raw/low_raw`
(NULL where unknown); `reprocess_instrument` rescales across the M29 seam; multi-leg purposes →
one action per leg, dividends summed; listings hide seam-day rows and `-RE*`; bounded redelivery
retry; catch-up stops re-proposing a `derived` holiday after one 404; NSE throttle mandatory +
cookie re-prime; explicit Kite timeout.
Scripts `ops/af/data/`: `measure.py` (read-only counts: source×date, instruments with nightly
kite rows, multi-leg action rows), `reparse.py`, `reprocess.py` (per-instrument, resumable,
`no_unexplained_jumps` after each), `verify.py`. All dry-run by default; `--apply` requires
`--backup-id`. Test them on the throwaway DB with fixtures.
**D2 (wave 2, after wave 1 ships):** backup per `docs/runbooks/restore-from-backup.md`, verify,
put the id in STATUS.md; `box-python.sh worker ops/af/data/measure.py`; `reparse.py` dry → apply;
`reprocess.py` dry → apply (any instrument failing the jump check stays unadjusted, listed in
STATUS.md); `verify.py`; if `/meta/status` is still degraded for 11 Sep, run the catch-up per
`docs/runbooks/pipeline-failed.md`. Not during market hours or 18:40–21:15 IST.

### E — Execution gateway and desk math
Owns: `packages/execution/**`, `packages/core/src/**/{score,basket,windows,factors,reference_export,trading_calendar}.py`,
`packages/core/src/**/models/{integrations,admin}.py`.
Items: 0.7, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10.
Fixed: CNC/derivative allow-list using `guards.DERIVATIVE_EXCHANGES` in `place` and
`place_gtt_stop`; `_sent` reserved under an `asyncio.Lock` before the first await and replayed
from the journal on construction; `assert_tradeable` returns BLOCKED; `math.isfinite` in
`refuse_stop`, `band_finding`, `place`; `gross_exposure` required, position cap accumulated,
`RiskState` persisted and IST-rolled; NULL in a filtered column → rejected (corpus in
`kite-momentum-rebalancer/data/uploads/*.csv` must stay green — if a corpus row disagrees, keep
the corpus and add one DECISIONS line); half-size rule implemented from listing date; `windows.py`
docstring/`bars_required` match `factors.py`; RSI gap → skip; `too_close` journalled only for
the weekly rebalancer; Law 1 leaks moved out of core. Desk suite green before you stop.

### F — Web plumbing: errors, routing, fetch, dead code
Owns: `apps/web/src/app/**/{error,not-found,loading,global-error}.tsx` (new), `apps/web/src/lib/{explore,basket,market}/fetch.ts`,
`apps/web/src/lib/api/{server-fetch,browser}.ts`, `apps/web/src/lib/brokers/fetch.ts`, `apps/web/src/app/actions/**`,
`apps/web/src/components/shell/section-tabs.tsx`, `apps/web/src/components/backtests/backtests-list.tsx`,
`apps/web/src/app/(app)/kitchen-sink/**`, `apps/web/src/lib/instrument/seo.ts`, `apps/web/src/lib/nav.ts`,
`apps/web/src/lib/screens/feature-flags.ts`, the three dead components, sleeve status colour classes
(`swing/**`, `vbt/**`, `twt/**` pages — colours only).
Items: 0.10, 1.7, 1.23 (kitchen-sink and `openapi.json` only), 4.2, 4.3, 4.4, 4.5, 4.7, 4.8, 4.9, 4.10.
Fixed: shelled `error`/`not-found`/`loading`/`global-error` under `(app)` and `(marketing)`;
404 vs unavailable split in explore/basket fetch; single-tab `aria-current`; backtest delete
confirm + error + `overflow-x-auto`; React `cache()` around loaders; timeouts on every action
and the browser client, none on `DELETE /me`; `Idempotency-Key` on `POST /portfolio` and
checkout; inert `revalidate` removed; instrument pages `index: false`; kitchen-sink 404 in
production; delete dead components, `planned` nav, legacy editor flag, sr-only test hook,
replace `as never` hrefs; palette classes → tokens.

### G — Web UI bugs and copy
Owns: `apps/web/src/app/(app)/basket/**`, `apps/web/src/components/basket/**`, `apps/web/src/components/cb/kite-basket-form.tsx`,
`apps/web/src/components/home/**`, `apps/web/src/app/(app)/portfolio/watchlist/**`, `apps/web/src/app/(app)/discover/**`
(copy/format only), `apps/web/src/app/(app)/brokers/**`, `apps/web/src/components/brokers/**`,
`apps/web/src/components/screens/**` (format only), `apps/web/src/app/(app)/build/backtests/page.tsx`,
`apps/web/src/components/backtests/config-form.tsx`, `apps/web/src/app/(marketing)/**`, `apps/web/src/app/(auth)/login/**`,
`apps/web/src/lib/{format,vocabulary}.ts`, `apps/web/src/lib/__tests__/copy-lint.test.ts`,
`services/api/src/baskfy_api/routers/{explore,curated_versions}.py` + `schemas.py` (`weight_pct`,
performance `coverage` only).
Items: 0.3, 1.1, 1.2, 1.5, 1.9, 1.10–1.22, 1.23 (the two orphan pages only), 1.24, 7.
Fixed: API `weight_pct` Decimal string, page renders it; version count per basket via
`box-sql.sh` → if the version job never ran, schedule it on Beat and label the basket until the
first new version; performance returns only covered points + `coverage`, chart draws real points;
Kite panel warns under min amount, lists zero-share drops, no horizontal overflow; header stat →
median of the top slice; one stat strip, "Read more", manager card without "(engine)"; copy-lint
rule fails the build on `docs/` in rendered strings; every §1 P2 and §7 string fixed as written;
history start read from `/meta/status.data_start_date` everywhere (one DECISIONS line naming
the published start); logged-out header drops Baskets/Market; delete `/discover/featured` and
`/discover/plan`.

### H — UX restructure (wave 2)
Owns: `apps/web/src/app/(app)/portfolio/**` pages, `apps/web/src/components/portfolio/**`,
`apps/web/src/components/investments/**`, `apps/web/src/components/cb/{plan-handoff-panel,market-closed-modal,invest-cta}.tsx`,
`apps/web/src/components/create/**`, `apps/web/src/app/(app)/build/page.tsx`, `apps/web/src/components/screens/screens-list.tsx`,
`apps/web/src/app/(app)/market/{today,listings}/**`, `apps/web/src/components/market/**`,
`apps/web/src/app/(app)/discover/{saved,all}/**`, `apps/web/src/components/shell/user-menu.tsx`,
`apps/web/src/app/(app)/home/**`, `apps/web/src/lib/nav.ts`.
Items: 5.1–5.13. Each is a product change: implement the version stated below, one DECISIONS line each.
Fixed: overview is the default portfolio page, command centre is a "Details" drill-down, same
tabs, one rupee format; "Not available" tiles → one line + tooltip, spec-gap sections deleted;
Invest more/Exit/Rebalance secondary until `/cb/plans/*` is wired, Kite hand-off primary where it
applies, "Notify me" deleted, market-closed line inline; Build hub: "Your screens", name prompt on
save, hide empty min amount, operator sleeves behind `is_staff`; Market Today headline strip
(NIFTY 50, Bank, Midcap 150, Smallcap 250, VIX) and tighter header; Saved → redirect to
Watchlist; catalogue `limit/offset`, listings Previous + total; `?screen=` pre-selects; Home hides
undifferentiated modules; user-menu operator groups behind `is_staff`; mobile leftovers.

### I — Small features (wave 2)
Owns: new files only, plus additive routes in `services/api/src/baskfy_api/routers/{curated_versions,explore,watchlist}.py`,
`apps/web/src/app/(app)/basket/[slug]/versions/**` (new), `docs/audit-fix/BACKLOG.md` (new, ≤ 15 lines).
Build: basket versions tab with diff between any two versions; instrument watchlist (save from
factsheet + screen table, "Stocks" section on Watchlist); Discover basket search (client-side);
first-login onboarding page (connect → import → pick a basket) with goal-composer choices saved
to the account; shareable basket link with OG image; honest Activity placeholder.
BACKLOG.md: one line each for manager onboarding, web invest/exit/rebalance plan (D3-gated),
CAS import + ledger, notifications centre, statements/tax export, fundamentals, brokers beyond
Zerodha, live prices product decision. Titles only. No briefs.

## Done means

`developer` merged, both suites + desk suite + `make lint` green, box runs HEAD
(`verify-pc-deploy.sh`), `ops/af/smoke.sh` passes against staging, D2 ran with a backup id (or
STATUS.md says why not), STATUS.md has a state for every item, `git status` clean with no
`.csv`/`.parquet`/`.dump` files, test Postgres down. Then stop — no closing summary.
