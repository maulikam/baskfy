# gates/sleeve-read-contract.md — do the three sleeves' writers and readers agree? (Agent C)

**Why this exists.** On 12 Sep 2026 the `/twt` page read *"Nothing has been read for this
strategy yet"* over a database holding 58 `tw_state_daily` rows, 2 `tw_signal_daily` rows and a
`tw_breadth_daily` row for user 1 / 2026-09-11. Agent R owned that read path. **This file is the
general case**: a writer and a reader that do not compute the session, or the tenant, with the
*same function* is a silent blindness — nothing errors, the page looks calm and empty, and the
person reading it concludes the job never ran.

**Scope.** The three sleeves (swing, VBT, TWT), both axes (date and user), both sides
(worker/desk writers, API + `apps/web` readers). Read-only against the box throughout.

> **STATUS, 12 Sep 2026 — five of the six are fixed (Agent F).** This file is the *audit*: its
> CHECK lines and EXPECT patterns describe the **red** state they were written to prove, so the
> ones for C1, C3, C5, C6, C7 and C7b no longer match — those tests now pass. That is the gate
> doing its job, not the gate breaking. The fixes, and the one disagreement deliberately left
> unfixed (**C4**, the desk's `BASKFY_SOLE_USER_ID` default of `1`, now a strict `xfail` carrying
> the reason and the recipe), are in **`gates/sleeve-contract-fixes.md`**. Nothing below was
> deleted or softened; read it as the record of what was true on the day it was measured.

---

## The table

| Sleeve | Writer's date rule | Reader's date rule | Writer's user rule | Reader's user rule | |
|---|---|---|---|---|---|
| **swing** | nightly: pipeline `trade_date` · beat `baskfy.swing.detect`: `now(IST).date()` · Scan now: `max(ohlcv_daily.date)`, else today-provisional | `max(sw_setup_daily.date) WHERE user_id` — **a table that is empty on any session with no candidate** | `providers._sole_user_id()`: env, else `None` → skip. Desk: `config.SOLE_USER_ID` = env **else `1`** | `scoped_sole_user_id` → `resolve_sole_user_id`: env, else `public_id='e2e000000001'`, else 503 | **DISAGREE** (C1, C3, C4, C5, C6, C7) |
| **VBT** | nightly: pipeline `trade_date` · beat: `now(IST).date()` · Scan now: `max(pipeline_run.trade_date) WHERE data_version IS NOT NULL` | `max(vb_breadth_daily.date) WHERE user_id` — written every session, signals or none | same as swing | same as swing | **date AGREE** (C2) · **user DISAGREE** (C4, C5, C6, C7) · `book()` caveat below |
| **TWT** | nightly: pipeline `trade_date` · beat: `now(IST).date()` · Scan now: `max(pipeline_run.trade_date) WHERE data_version IS NOT NULL` | `max(tw_breadth_daily.date) WHERE user_id` — **as of Agent R's `GET /twt/today`, 12 Sep 2026**; before that there was **no reader at all** | same as swing | same as swing | **date AGREE now** (C8) · **user DISAGREE** (C4, C5, C6, C7) |

### The disagreements, named

* **C1 — swing, date.** The reader's clock hangs off `sw_setup_daily`, which the detector leaves
  empty on a session with no candidate, while `write_market_row` runs unconditionally. So a
  no-flags session serves **yesterday's triggers and yesterday's gate** stamped with yesterday's
  date. VBT and TWT key theirs on the breadth table and do not have it. On the box the gate has
  been RED since 2026-09-04 and the candidate count has walked 21 → 22 → 16 → 19 → 12 → **9**.
* **C3 — the three "Scan now" writers, date.** "The last published session" is two queries:
  swing reads `max(ohlcv_daily.date)`; VBT and TWT read `max(pipeline_run.trade_date) WHERE
  data_version IS NOT NULL`. Bars exist before a run is published, so they diverge exactly when
  the data quality gate refuses a day — `pipeline_run` 48 on the box, 2026-09-11:
  `fetch_daily_bars succeeded rows_out=4358`, then `data_quality_gate failed`, `data_version`
  NULL until the 22:49 re-run. For four hours a swing scan would have re-detected a refused
  session while VBT's and TWT's correctly answered 2026-09-10.
* **C4 — every sleeve, user, and the dangerous one.** `BASKFY_SOLE_USER_ID` is read in five
  places and each answers something different when it is unset: worker → `None` (skip);
  `resolve_sole_user_id` → the e2e account, else 503; `seed._sole_user_id` → `min(app_user.id)`;
  **`kite-momentum-rebalancer/app/config.py` → the literal `1`**; `swing_monitor` → `0`, refuse.
  **`C.SOLE_USER_ID` is what the desk's three stores are opened with** —
  `swing_desk.py:1481`, `vbt_desk.py:827`, `twt_desk.py:1018`, each
  `PgSwingStore/…(conn, user_id=C.SOLE_USER_ID, broker_account_id=C.SOLE_BROKER_ACCOUNT_ID)`
  — so the fallback is not a display default, it is the tenant on every `tw_order`,
  `sw_position` and confirmed plan line the desk writes.
  The worker's own docstring forbids exactly what the desk does — *"`None` rather than a default
  of 1 … inventing a tenant for a deployment that never declared one would write another
  account's book"* — and the desk is the half that reaches the gateway. `app_user` on the box
  holds six accounts with portfolios for users **1 and 6**.
* **C5 / C6 — every sleeve, user.** `BASKFY_SOLE_USER_ID=` (blank) or mistyped: the worker
  strips and skips quietly, the API's `int(env_val)` raises `ValueError` out of a request
  handler (a 500, not the 503 its own "not configured" branch exists to give), and the desk
  raises at import. One typo, three failure modes, one of them silent.
* **C7 — every sleeve, user, live today.** `scoped_sole_user_id` 404s a principal who is not the
  sole tenant, which is right (M43.4). One layer up, every sleeve's `fetch.ts` funnels **every**
  non-OK response through `readOrNull` → `null`, and every page renders `null` as its empty
  state. A second signed-in account is told the strategy has never run. The refusal's own
  problem detail says `No watchlist with id '6'` on `/swing/setups`, so the server log points at
  the curated-basket watchlist rather than the swing book.

### Not a disagreement, but worth writing down

* `baskfy_api.vbt.book()` falls back to `dt.date.today()` — the container's wall clock, not the
  published-session clock the rest of the sleeve uses and not the IST constant the writers use.
  It is currently harmless *because* the box's api container runs `TZ=Asia/Kolkata` (verified),
  which makes a code rule depend on a deployment setting. Reached only when the detector has
  written no breadth row at all, so it is a note rather than a ledger row.
* The swing hub renders **three independent clocks on one page** and none of them is told
  about the others: `latest_setup_date` = `max(sw_setup_daily.date)` (the setups and the gate),
  `latest_market_date` = `max(sw_market_daily.date)` (the market history), and
  `latest_signal_date` = `max(sw_signal.session_date)` (the monitor's intraday verdicts, written
  by the desk, not by the nightly). Three `max()`es over three tables written by two processes.
  They agree on any ordinary session, which is why this is a note; C1 is the case where the
  first two come apart, and it is the one that changes what the page says.
* The brief for this audit said `BASKFY_SOLE_USER_ID` is **UNSET** on the production box. It is
  not: `.env.staging` carries `BASKFY_SOLE_USER_ID=1` and both the `api` and `worker` containers
  read `1` at runtime (C10). That is *why* everything on the box currently agrees, and why C4–C7
  are latent rather than live. The premise was checked rather than built against — root
  `CLAUDE.md`, "when the code and a doc disagree".

---

## The ledger

- [x] C1: The swing reader's `as_of` is the last session with a *candidate*, not the last
      session the detector ran — and the test says so.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentC uv run pytest "services/api/tests/test_sleeve_read_contract.py::TestTheReadersDate::test_swing_as_of_is_the_session_the_detector_ran" --no-header -p no:randomly 2>&1 | tail -60
  EXPECT: /the swing page is stamped with the last session that produced a candidate/
  EVIDENCE: `1 failed`. The assertion prints "the swing page is stamped with the last session that produced a candidate, not the last session the detector ran". `baskfy_api.swing.latest_setup_date` is `select(func.max(SwSetupDaily.date))`; `baskfy_worker.tasks.swing._detect_swing` calls `write_market_row` after `_upsert_setups` whether or not `candidates.is_empty()`.

- [x] C2: VBT is the control — its reader keys on the always-written breadth row and agrees
      with its writer under the same fixture.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentC uv run pytest "services/api/tests/test_sleeve_read_contract.py::TestTheReadersDate::test_vbt_as_of_is_the_session_the_detector_ran" --no-header -p no:randomly 2>&1 | tail -60
  EXPECT: /1 passed/m
  EVIDENCE: `1 passed`. Same fixture shape as C1 — a signal on the previous session, a breadth row on both — and `/vbt/today` answers the *later* date. The fix for C1 is not an invention; it is the rule the neighbouring sleeve already follows.

- [x] C3: The three "Scan now" writers do not share one answer to "what is the last published
      session".
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentC uv run pytest "services/api/tests/test_sleeve_read_contract.py::TestTheScanWritersSession" --no-header -p no:randomly 2>&1 | tail -60
  EXPECT: /do not share one answer/
  EVIDENCE: `1 failed`, printing `{'swing': datetime.date(2026, 8, 18), 'vbt': datetime.date(2026, 8, 17), 'twt': datetime.date(2026, 8, 17)}` for a fixture where the bars landed and the quality gate refused the day.

- [x] C3b: The box shows the state C3 needs is real, not hypothetical — bars written, quality
      gate failed, `data_version` NULL.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=baskfy-poc BOX_SQL_LINES=30 bash tools/deploy/box-sql.sh "select r.id||' '||s.step||' '||s.status||' '||coalesce(s.rows_out,0) from pipeline_run r join pipeline_run_step s on s.run_id=r.id where r.trade_date='2026-09-11' and r.data_version is null and s.step in ('fetch_daily_bars','data_quality_gate') order by r.id, s.id"
  EXPECT: /fetch_daily_bars succeeded 4358[\s\S]*data_quality_gate failed/m
  EVIDENCE: `48 fetch_daily_bars succeeded 4358` / `48 data_quality_gate failed 6`. Run 48 started 18:45 IST; the published run (`data_version` 18) is the 22:49 one.

- [x] C4: A module defaults `BASKFY_SOLE_USER_ID` to a real account id instead of refusing to
      guess — and it is the desk, the half that reaches the gateway.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentC uv run pytest "services/api/tests/test_sleeve_read_contract.py::TestTheTenant::test_no_module_invents_a_tenant_when_the_variable_is_unset" --no-header -p no:randomly 2>&1 | tail -60
  EXPECT: /defaults BASKFY_SOLE_USER_ID to a real account id/
  EVIDENCE: `1 failed`, naming `desk: SOLE_USER_ID = int(os.getenv("BASKFY_SOLE_USER_ID", "1"))` (`kite-momentum-rebalancer/app/config.py:15`).

- [x] C4b: The five readers of the variable, each with its own unset answer, all still exist.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -hE 'os\.(environ\.get|getenv)\((SOLE_USER_ENV|"BASKFY_SOLE_USER_ID")' decile-blueprint/services/worker/src/baskfy_worker/providers.py decile-blueprint/services/api/src/baskfy_api/curated_seed.py decile-blueprint/services/api/src/baskfy_api/seed.py kite-momentum-rebalancer/app/config.py kite-momentum-rebalancer/app/swing_monitor.py
  EXPECT: /"BASKFY_SOLE_USER_ID", ""\)\.strip\(\)[\s\S]*os\.getenv\("BASKFY_SOLE_USER_ID", "1"\)[\s\S]*"BASKFY_SOLE_USER_ID", "0"\)/m
  EVIDENCE: five lines, side by side, with three different defaults on them: `os.environ.get("BASKFY_SOLE_USER_ID", "").strip()` (worker: blank means no tenant), `os.environ.get(SOLE_USER_ENV)` (api: unset falls through to the e2e account), `os.environ.get("BASKFY_SOLE_USER_ID")` (seed: unset falls through to `min(app_user.id)`), `int(os.getenv("BASKFY_SOLE_USER_ID", "1"))` (desk: unset **is user 1**), `int(os.environ.get("BASKFY_SOLE_USER_ID", "0") or 0)` (monitor: unset is 0, then refuse).

- [x] C5: A blank `BASKFY_SOLE_USER_ID` means "no tenant" to the writer and a `ValueError` to
      the reader.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentC uv run pytest "services/api/tests/test_sleeve_read_contract.py::TestTheTenant::test_a_blank_variable_means_the_same_thing_to_the_writer_and_the_reader" --no-header -p no:randomly 2>&1 | tail -60
  EXPECT: /reads a blank BASKFY_SOLE_USER_ID as a number and raises ValueError/
  EVIDENCE: `1 failed`. `providers._sole_user_id` does `os.environ.get(..., "").strip()` then `if not raw: return None`; `curated_seed.resolve_sole_user_id` does `os.environ.get(...)` then `int(env_val)` with no strip and no empty check.

- [x] C6: The same crack, widened — a mistyped value is a warning-and-skip in the worker and an
      unhandled exception in the API.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentC uv run pytest "services/api/tests/test_sleeve_read_contract.py::TestTheTenant::test_a_non_numeric_variable_means_the_same_thing_to_both" --no-header -p no:randomly 2>&1 | tail -60
  EXPECT: /a mistyped BASKFY_SOLE_USER_ID is a warning-and-skip in the worker and an unhandled/
  EVIDENCE: `1 failed`. The worker logs "BASKFY_SOLE_USER_ID is not a number (%r); the swing step will skip" and returns `None`; the API raises out of the request.

- [x] C7: The API's refusal of a second account names the wrong surface.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentC uv run pytest "services/api/tests/test_sleeve_read_contract.py::TestASecondAccount" --no-header -p no:randomly 2>&1 | tail -60
  EXPECT: /the refusal names a surface the caller never asked for/
  EVIDENCE: `1 failed`. `GET /api/v1/swing/setups` with a non-sole account answers `{"type":"not-found","detail":"No watchlist with id '4'."}` — `curated_tenant.scoped_sole_user_id` raises `not_found("watchlist", ...)` whatever route it guards.

- [x] C7b: And the page cannot tell that refusal from an empty database — every sleeve's
      `readOrNull` collapses a 404 and a 503 to the same `null` that means "the detector has
      never run".
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm vitest run src/lib/swing/__tests__/read-contract.test.ts 2>&1 | tail -6
  EXPECT: /2 failed \| 1 passed/
  EVIDENCE: the control ("answers null when the detector has genuinely written nothing") passes; the 404 case and the 503 case both fail, because `fetchSetups` answers `null` to all three.

- [x] C8: TWT now has a reader, and it keys its session on the always-written breadth row —
      the C1 rule, not the C1 bug.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentC uv run pytest "services/api/tests/test_sleeve_read_contract.py::TestTheTwtReader" --no-header -p no:randomly 2>&1 | tail -60 ; grep -c 'TwBreadthDaily.date.desc()' services/api/src/baskfy_api/twt.py
  EXPECT: /1 passed[\s\S]*^1$/m
  EVIDENCE: Agent R landed `GET /twt/today` in `routers/twt.py` + `baskfy_api/twt.py` on 12 Sep 2026, and `_latest` orders `TwBreadthDaily.date.desc()`. Had it keyed on `tw_signal_daily` the page would have served a session from *last month* — this strategy signals about eighteen times a year — and looked current doing it.

- [x] C9: Box evidence — every writer on the box wrote **user 1**, and the newest session they
      wrote is 2026-09-11 on all three sleeves.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=baskfy-poc BOX_SQL_LINES=30 bash tools/deploy/box-sql.sh "select t||' u'||user_id||' '||d from (select 'sw_setup' t, user_id, max(date)::text d from sw_setup_daily group by 1,2 union all select 'vb_signal', user_id, max(date)::text from vb_signal_daily group by 1,2 union all select 'tw_state', user_id, max(date)::text from tw_state_daily group by 1,2) x order by 1"
  EXPECT: /sw_setup u1 2026-09-11[\s\S]*tw_state u1 2026-09-11[\s\S]*vb_signal u1 2026-09-11/m
  EVIDENCE: `sw_setup u1 2026-09-11`, `tw_state u1 2026-09-11`, `vb_signal u1 2026-09-11`. 157 / 37 / 58 rows respectively; `sw_config`, `vb_config` and `tw_config` all belong to user 1.

- [x] C10: The brief's premise was wrong and was checked rather than built against —
      `BASKFY_SOLE_USER_ID` is **set to 1** on the box, in both the api and the worker.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=baskfy-poc bash tools/deploy/box.sh "cd /opt/baskfy && grep '^BASKFY_SOLE_USER_ID' .env.staging && docker compose -f compose.prod.yml --env-file .env.staging.compose exec -T api sh -lc 'echo API=[\$BASKFY_SOLE_USER_ID]'" 2>/dev/null | tail -4
  EXPECT: /BASKFY_SOLE_USER_ID=1[\s\S]*API=\[1\]/m
  EVIDENCE: `BASKFY_SOLE_USER_ID=1`; `API=[1]`; `WORKER=[1]` on the same check. Both containers take `env_file: [.env.staging]` from the shared `x-py` anchor in `compose.prod.yml`, so they cannot diverge — which is exactly why C4–C7 are latent on this box rather than live.

- [x] C11: No other account can be served the sole tenant's book — the audit did not loosen
      anything it found.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentC uv run pytest services/api/tests/test_curated_tenant_isolation.py services/api/tests/test_api_vbt.py services/api/tests/test_api_swing.py --no-header -p no:randomly 2>&1 | tail -3
  EXPECT: /^\d+ passed in /m
  EVIDENCE: `74 passed`. pytest prints a bare `N passed` line only when nothing failed, so this gate goes red the moment the audit's edits break a neighbour. The audit added one test file and one vitest file and changed no source.

- [x] C12: The whole audit suite runs, and the count of proved disagreements is six.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentC uv run pytest services/api/tests/test_sleeve_read_contract.py --no-header -p no:randomly 2>&1 | tail -2
  EXPECT: /6 failed, 2 passed/
  EVIDENCE: six failing tests, one per named disagreement (C1, C3, C4, C5, C6, C7); two passing, the VBT date control (C2) and the TWT reader Agent R landed (C8).
