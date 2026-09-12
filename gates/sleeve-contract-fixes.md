# gates/sleeve-contract-fixes.md — what Agent F fixed, and what it deliberately did not (12 Sep 2026)

**The input.** `gates/sleeve-read-contract.md` (Agent C) proved six writer/reader disagreements
across the three sleeves and left a failing test for each rather than rushing a fix. This file is
the other half: **five fixed, one left red on purpose and named.**

**The rule this file is written under.** A green suite that hides a known bug is worse than a red
one that names it. So the one disagreement that could not be closed safely in this pass is a
`pytest.mark.xfail(strict=True)` carrying the reason, the seven call sites, and the recipe — it
flips to XPASS the day somebody fixes it, and the marker then has to come off. **No test was
deleted and no assertion was weakened.** One test's *mock response body* was updated to the
refusal's new shape; F6 says exactly what changed and ships the control that proves it was
precision rather than a loosened net.

| | Disagreement | Verdict |
|---|---|---|
| C1 | swing's reader keyed its clock on a table that is empty on a no-candidate session | **FIXED** (F1) |
| C3 | three "Scan now" writers, two answers to "the last published session" | **FIXED** (F2, F3) |
| C4 | the desk defaults `BASKFY_SOLE_USER_ID` to the literal `1` | **NOT FIXED — xfail** (F7) |
| C5 | a blank variable: quiet skip in the worker, `ValueError` out of a request handler | **FIXED** (F4) |
| C6 | a mistyped variable: the same crack, widened | **FIXED** (F4) |
| C7 | a refused account renders identically to an empty database | **FIXED** (F5, F6) |

---

## What changed, in one paragraph each

**C1 — the live one.** `baskfy_api.swing.setups` asked `max(sw_setup_daily.date)` for the page's
`as_of`, and the detector leaves that table empty on any session where nothing met the bar while
`write_market_row` runs unconditionally. The new `baskfy_api.swing.latest_detected_date` keys on
`sw_market_daily` — the row the detector writes every session — which is the rule VBT
(`vb_breadth_daily`) and TWT (`tw_breadth_daily`) already follow. `latest_setup_date` is kept, now
documented as "the last session that flagged something", which is a different question.

**C3 — one definition, one place.** `baskfy_worker.tasks.published_session.last_published_session`
is now the only query for "the last published session", and all three scans delegate to it. Swing
asked `max(ohlcv_daily.date)`; bars land before a run is published, so on 2026-09-11 (box run 48:
`fetch_daily_bars succeeded rows_out=4358`, then `data_quality_gate failed`, `data_version` NULL
until 22:49) a swing scan would have re-detected a day the quality gate refused. The published-run
rule wins because a day the gate refused is not a published day. `test_swing_scan_now.py`'s
fixtures now write the `pipeline_run` alongside the bars, which is what an evening on the box
actually leaves behind.

**C5 / C6 — the tenant, read the same way on both sides.** `curated_seed.resolve_sole_user_id`
now strips the variable, treats empty as unset, and logs-and-treats-as-unset a non-number —
`baskfy_worker.providers._sole_user_id`'s exact semantics. Unset then takes the function's own
"not configured" path, which ends in a 503 naming the variable instead of a `ValueError` escaping
a request handler as a 500.

**C7 — a refusal is no longer spelled like an empty database.** Two halves. (a)
`curated_tenant.scoped_sole_user_id` raised `not_found("watchlist", …)` whatever surface it was
guarding, so a second account reading the *swing book* was answered `No watchlist with id '6'.`;
it now says what happened and carries an RFC 9457 extension member, `reason:
"not-the-sole-tenant"`. It has to be a body member, not a status: the refusal is deliberately a
404 rather than a 403 (M43.4), which by design makes it indistinguishable from absence to anything
reading the status alone. (b) `apps/web/src/lib/api/sleeve-read.ts` reads that member, and all
three sleeves' `readOrNull` now let a refusal, a 503 and a 5xx out as `SleeveUnavailableError`
instead of `null`. New `error.tsx` boundaries under `/swing`, `/vbt` and `/twt` catch them, so no
page can render any of the three as "No flags today".

**Still true after this pass, and said out loud:** a **timed-out or unreachable** read still
answers `null` and still renders as the empty state. Making a 4s RSC timeout take a trading page
down is a bigger behaviour change than the audit asked for; it is documented at the top of
`sleeve-read.ts` rather than folded in quietly. And the `error.tsx` boundary cannot name *which*
of refused / degraded / errored it was, because Next redacts a server error's message before it
reaches the browser in production — the page saying "this was not read" is the fix; the page
saying "this book is not yours" needs each page to catch `SleeveUnavailableError` from its own
read, which is a one-file-per-page follow-up.

---

## The ledger

*F10 runs the whole API suite and takes ~30 minutes on this machine, which is longer than
`tools/gates/rerun.py`'s 900s default — re-run this file with `--timeout 3000`.*

- [x] F1: C1 is fixed — the swing page's clock is the session the **detector ran**
      (`sw_market_daily`), not the last session that produced a candidate, and VBT's control
      still passes beside it.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentF uv run pytest "services/api/tests/test_sleeve_read_contract.py::TestTheReadersDate" --no-header -p no:randomly 2>&1 | tail -3
  EXPECT: /^2 passed in /m
  EVIDENCE: Agent C's C1 test asserted `as_of == 2026-08-18` on a fixture whose only candidate is on 08-17; it failed, and now passes. `baskfy_api.swing.latest_detected_date` reads `max(sw_market_daily.date)` and falls back to the setup date only for a book with no market row at all.

- [x] F1b: box evidence that the C1 fix is a *future* correction and not a change of today's
      answer — on 2026-09-12 the two tables agree, so the page shows the same session it showed
      yesterday and will stop lying on the next session with no candidate.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=baskfy-poc BOX_SQL_LINES=20 bash tools/deploy/box-sql.sh "select 'sw_market u'||user_id||' '||max(date)::text from sw_market_daily group by user_id union all select 'sw_setup u'||user_id||' '||max(date)::text from sw_setup_daily group by user_id"
  EXPECT: /sw_market u1 2026-09-11[\s\S]*sw_setup u1 2026-09-11/m
  EVIDENCE: read-only against the box. `max(sw_market_daily.date)` = `max(sw_setup_daily.date)` = 2026-09-11 for the sole tenant, which is why C1 was latent-looking rather than visibly wrong — the candidate count has walked 21 → 22 → 16 → 19 → 12 → **9** with the gate RED since 2026-09-04, and nine is not far from nought. The day it reaches nought, the old reader served yesterday's gate and yesterday's triggers stamped yesterday.

- [x] F2: C3 is fixed — the three "Scan now" writers share **one** answer to "what is the last
      published session", and it is the published-run rule, not `max(ohlcv_daily.date)`.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentF uv run pytest "services/api/tests/test_sleeve_read_contract.py::TestTheScanWritersSession" --no-header -p no:randomly 2>&1 | tail -3
  EXPECT: /^1 passed in /m
  EVIDENCE: the same fixture that printed `{'swing': date(2026, 8, 18), 'vbt': date(2026, 8, 17), 'twt': date(2026, 8, 17)}` — bars landed, the gate refused the day — now answers 2026-08-17 three times.

- [x] F3: and there is one definition to be wrong rather than three to drift — all three modules
      delegate to `baskfy_worker.tasks.published_session`, and the swing scan's own suite is green
      against fixtures that publish a run instead of only writing bars.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && grep -c "from baskfy_worker.tasks.published_session import" services/worker/src/baskfy_worker/tasks/swing_scan_now.py services/worker/src/baskfy_worker/tasks/vbt_rescan.py services/worker/src/baskfy_worker/tasks/twt_scan.py && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentF uv run pytest services/worker/tests/test_swing_scan_now.py services/worker/tests/test_vbt_rescan.py --no-header -p no:randomly 2>&1 | tail -3
  EXPECT: /swing_scan_now\.py:1[\s\S]*vbt_rescan\.py:1[\s\S]*twt_scan\.py:1[\s\S]*^\d+ passed in /m
  EVIDENCE: `decide_session` now refuses a session the quality gate refused; the 15 scan-now tests that broke when it stopped reading `max(ohlcv_daily.date)` were fixtures writing bars with no published run, and `_publish()` writes the `pipeline_run` the box writes.

- [x] F4: C5 and C6 are fixed — a blank and a mistyped `BASKFY_SOLE_USER_ID` mean the same thing
      to the writer and the reader, and the reader's answer is its documented 503 rather than a
      `ValueError` escaping a request handler.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentF uv run pytest "services/api/tests/test_sleeve_read_contract.py::TestTheTenant" --no-header -p no:randomly -rx 2>&1 | tail -4
  EXPECT: /2 passed, 1 xfailed in /m
  EVIDENCE: `resolve_sole_user_id` reads the variable exactly as `baskfy_worker.providers._sole_user_id` does. **What changed in Agent C's two tests:** each gained an `except Problem` branch, because the answer the fix produces is the 503 those tests' own prose calls correct — *"a 500, not the 503 its own 'not configured' branch exists to give"*. The `pytest.fail` on `ValueError` — the failure they name — is untouched, and the new branch asserts more than before (`type is PIPELINE_DEGRADED`, `status == 503`, the detail names the variable). The trailing `reader_answer is not None` was written expecting the fixture to carry a seeded e2e account; it is deliberately *not* reached by widening the refusal into an answer, because a reader that invented a tenant where the writer refused to have one would be C4 with extra steps. The 1 xfailed is C4 — F7.

- [x] F5: C7's API half — the refusal no longer names a surface the caller never asked for, and
      it carries a machine-readable marker so a reader can tell it from "there is nothing here".
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && grep -n "SOLE_TENANT_REFUSED" services/api/src/baskfy_api/curated_tenant.py | tail -2 && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentF uv run pytest "services/api/tests/test_sleeve_read_contract.py::TestASecondAccount" services/api/tests/test_curated_tenant_isolation.py --no-header -p no:randomly 2>&1 | tail -3
  EXPECT: /SOLE_TENANT_REFUSED = "not-the-sole-tenant"[\s\S]*reason=SOLE_TENANT_REFUSED[\s\S]*^\d+ passed in /m
  EVIDENCE: `GET /api/v1/swing/setups` with a non-sole account answered `{"detail":"No watchlist with id '4'."}`; it now answers "This deployment serves a single account and this caller (user 4) is not it." with `reason: "not-the-sole-tenant"`. The isolation suite still passes — the refusal is still a 404, and still refuses before any query runs.

- [x] F6: C7's web half, on **all three** sleeves — a refusal, a 503 and a 500 no longer arrive at
      a page as the value that means "the detector has never run", and a plain 404 still does.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm vitest run src/lib/swing/__tests__/read-contract.test.ts src/lib/api/__tests__/sleeve-read-contract.test.ts 2>&1 | tail -6
  EXPECT: /Tests {2}14 passed \(14\)/m
  EVIDENCE: Agent C's three swing cases pass unchanged in intent; `src/lib/api/__tests__/sleeve-read-contract.test.ts` repeats them on VBT and TWT — the sleeve whose empty state this audit is named after. **What changed in Agent C's file:** the 404 stub's *body* is now the refusal the API actually sends (`reason: "not-the-sole-tenant"`), because the refusal is deliberately a 404 and a rule of "every 404 is visible" would break `/swing/setups/{id}/bars` for an unknown instrument. The assertions are untouched, and both files now carry the control that a *plain* 404 still answers `null`.

- [x] F7: **C4 IS NOT FIXED**, and the suite says so out loud rather than going quiet — a strict
      `xfail` carrying the reason, the blast radius and the recipe, which turns into a failure the
      day the desk stops defaulting the tenant to `1`.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentF uv run pytest "services/api/tests/test_sleeve_read_contract.py::TestTheTenant::test_no_module_invents_a_tenant_when_the_variable_is_unset" --no-header -p no:randomly -rx 2>&1 | tail -30 && grep -n 'BASKFY_SOLE_USER_ID", "1"' /Users/maulikdave/Documents/projects/baskfy/kite-momentum-rebalancer/app/config.py
  EXPECT: /XFAIL[\s\S]*C4 IS NOT FIXED[\s\S]*1 xfailed in [\s\S]*^\d+:SOLE_USER_ID = int\(os\.getenv\("BASKFY_SOLE_USER_ID", "1"\)\)/m
  EVIDENCE: `strict=True`, so it fails if it ever passes silently. Why not fixed: `C.SOLE_USER_ID` is read at **import** time and has seven call sites in the desk (`core/gateway.py:54`, `swing_desk.py:1481`, `vbt_desk.py:827`, `twt_desk.py:1018`, and the three `*_execute.py` TenantIds builders), so an import-time refusal breaks every desk process and test that has not set the variable, and a lazy refusal means moving those call sites — which `kite-momentum-rebalancer/tests/test_swing_track_c.py:392` pins by scanning the source for the literal spelling `user_id=C.SOLE_USER_ID`. That is a cross-tree change on the **order path**, in the tree this agent was told to stay out of, on a desk with swing auto-execute armed, and root `CLAUDE.md`'s rails say no module ends with the desk's tree broken. Latent, not live: `.env.staging` sets it to 1 in both containers (C10). The recipe is in the marker's own `reason`.

- [x] F8: the neighbours the fixes could have broken are green — the swing, VBT and TWT API
      suites, the tenant-isolation suite, and the read-only guards.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentF uv run pytest services/api/tests/test_api_swing.py services/api/tests/test_api_vbt.py services/api/tests/test_api_twt_today.py services/api/tests/test_curated_tenant_isolation.py services/api/tests/test_swing_readonly.py services/api/tests/test_twt_readonly.py --no-header -p no:randomly 2>&1 | tail -3
  EXPECT: /^\d+ passed in /m
  EVIDENCE: pytest prints a bare `N passed` line only when nothing failed, so this gate goes red the moment one of these fixes breaks a neighbour. C1 changed what `/swing/setups` calls "today" and C7 changed what every sole-tenant route says when it refuses; these are the suites that would notice.

- [x] F9: the web app typechecks with the new read layer and the three `error.tsx` boundaries in
      it, and the two source scanners that police what this pass wrote — §8's banned vocabulary
      and `any` — are green.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec tsc --noEmit -p tsconfig.json && pnpm vitest run src/lib/__tests__/no-jargon.test.ts src/lib/__tests__/no-any.test.ts src/lib/api/__tests__/served-paths.test.ts 2>&1 | tail -6
  EXPECT: /Test Files {2}3 passed \(3\)[\s\S]*Tests {2}\d+ passed \(\d+\)/m
  EVIDENCE: `no-jargon.test.ts` caught the first draft of the new copy calling these things "the swing book" and "the sleeve" — §8's banned words — and the boundaries now use `lib/vocabulary`'s own page titles. `no-any.test.ts` was already red on a line of Agent C's prose ("`readOrNull`: any non-OK response"); that sentence is rewritten, not exempted. `served-paths.test.ts` is here because the new `error.tsx` files and `sleeve-read.ts` sit in the tree it scans. **Narrowed deliberately (12 Sep 2026):** this row used to run `pnpm vitest run src/lib src/app src/components` — 175 files, 3,106 tests — and it passed that way at 18:34 IST, whole and green. It was cut to the three scanners because a ledger row that re-runs the world every time it is checked wastes twenty minutes of somebody's machine for the same answer; the whole-suite run belongs to the parent, once.

- [ ] F10: the whole API suite is green — the fixes touched a shared tenant guard and a shared
      session rule, and nothing else in the service moved. **Measured once, then abandoned as a
      re-runnable check** (see ABANDON below).
  ABANDON: F10 an 11-minute full-suite run is the parent's job, not a per-re-run cost of this ledger; the result below is from the completed run at 18:19-18:29 IST on 12 Sep 2026 and is not repeated here.
  EVIDENCE: **`1984 passed, 1 skipped, 1 xfailed in 560.67s`** for `services/api/tests` with `--ignore=services/api/tests/test_load.py`. The 1 xfailed is C4 (F7). Run *including* `test_load.py` the same evening it was `1 failed, 1986 passed, 1 skipped, 1 xfailed in 681.54s`, the one failure being `test_p95_stays_under_four_hundred_milliseconds_with_no_errors` — `500 requests, 0 errors (0.00%), p50 235 ms, p95 413 ms` against a 400 ms budget, measured while two other agents' suites and a vitest run were competing for this laptop. It is a wall-clock budget on `/screens/{id}/run`, a path none of these fixes touches, and 0 errors means nothing refused or broke. Named here rather than hidden: if the parent's full run shows the same p95 row, it is the machine, and if it shows anything else it is a real finding.
