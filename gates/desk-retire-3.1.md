# Gates: 3.1 A resync button — find what is pending and fix it, from the browser

Scope: Maulik must not have to reach this machine to repair data. One button that pulls the
Kite session, finds every gap, closes what it can, and reports honestly on what it could not.

Why the gap detector is the hard part, and must not be "has any bars?": on 2026-02-01 the box
held 322 bars against 2,310 on the neighbouring session — a Budget Sunday that Kite's
deep-history pass silently skipped. NSE had published a full 3,229-row bhavcopy the whole time.
A presence check calls that day fine. It was 87% missing and it corrupted every 9M/12M window
that crossed it. Separately, 2026-08-28 was a real trading day wrongly marked a holiday by
inference after a failed fetch (M62). **Both classes must be detected.**

- [x] G1: An endpoint finds pending work without changing anything — a dry inspection
  CHECK: grep -rc "resync" decile-blueprint/services/api/src/baskfy_api/routers/admin.py
  EXPECT: /[1-9]/
  EVIDENCE: `GET /api/v1/admin/resync` (`routers/admin.py:225`, `inspect_resync`) answers
  `ResyncPlanOut` synchronously; the acting half is a separate `POST` on the same path
  (`routers/admin.py:268`, `start_resync`). The CHECK measures 15 hits of "resync" in that file.
  "Changes nothing" is asserted, not claimed: `test_admin_resync.py`
  ::`TestTheInspectionChangesNothing::test_it_writes_no_row` snapshots
  `(count(ohlcv_daily), count(trading_day), count(admin_action))` either side of an inspection and
  asserts equality; `::test_two_inspections_in_a_row_agree` asserts the two plans compare equal.
  Measured against the **live staging database** (read-only probe, `docker exec
  baskfy-staging-api-1`): `window 2025-07-27 .. 2026-08-31 · trading days 271 · elapsed 202 ms ·
  pending True (6 findings)`.

- [x] G2: The detector catches ALL FOUR gap classes, each with a test:
      (a) a trading day with no succeeded pipeline_run
      (b) a trading day whose bar count is far below its neighbours (the 1 Feb class)
      (c) a weekday marked a holiday by inference for which NSE did publish a bhavcopy (M62)
      (d) a stale or absent Kite session
  EVIDENCE: `baskfy_api.resync.ResyncKind` names the four —
  `missing_run` / `thin_bars` / `wrong_holiday` / `kite_session`. 28 tests in
  `services/api/tests/test_admin_resync.py`, all green
  (`uv run pytest services/api/tests/test_admin_resync.py -q` → `28 passed`):

  * **(a)** `TestClassAMissingRun` — three tests. A trading day whose run row is deleted is
    reported; a run that *succeeded but failed the gate* (`data_version IS NULL`) does **not**
    count as published; and days before the pipeline's first-ever run are deliberately not
    reported (staging's `pipeline_run` starts 2026-08-18 over bars reaching 2024 — 150 correct
    backfilled days would bury the one that matters).
  * **(b)** `TestClassBThinBars` — four tests. A day cut to 25 bars against a neighbouring median
    of 180 is reported **with `observed_bars > 0`**, i.e. a presence check would have passed it;
    a day at 90% of the median is not reported; a 3× inflated neighbour does not let the thin day
    hide (the median, not the mean); an empty window is not a wall of thin days.
    **Threshold, and why:** `< 0.60 × the median of the ten nearest sessions that have bars`,
    floored at a median of 100. A fraction of a *median* rather than an absolute number because
    the universe grows (the box held 2,550 bars a session in mid-July 2026 and 3,014 six weeks
    later) and because 2026-08-31 carries 9,225 rows — three times its neighbours — which a mean
    would let a genuinely thin day sit beside. **Replayed over the real staging series, 271
    sessions 2025-07-28 → 2026-08-31:** 0 flagged as the box stands today (no false positives);
    2026-02-01 restored to its real 322 bars flags at `ratio=0.139` against a neighbouring median
    of 2,313; 2026-08-28 with the failed fetch's 0 bars flags at `ratio=0.0` against 2,986.
  * **(c)** `TestClassCWrongInferredHoliday` — four tests. A weekday marked
    `holiday_name='inferred: no instrument traded'` that NSE published for is reported; one NSE
    published nothing for stays a holiday; a `source='holiday'` circular holiday is never
    second-guessed; and a host that *cannot ask NSE* reports that fact rather than a clean plan.
    The probe is `baskfy_providers.publication.bhavcopy_publication_check` — M62's own
    `published` callable, moved down from `baskfy_worker.orchestrator` so the API and
    `reconcile_calendar` ask NSE through one piece of code rather than two.
    **Against live staging with the real NSE provider attached:** all 9 weekdays in the window
    recorded as inferred holidays were probed and none flagged — they are genuine lunar-calendar
    holidays. Zero false positives on production data.
  * **(d)** `TestClassDKiteSession` — four tests over a real Fernet-encrypted store: an absent
    token is pending; a token issued 2026-08-27 read at 2026-08-28 is pending (a Kite token dies
    at the next pre-open); a token issued today is not; and a host with no store configured says
    so instead of reporting health. Arithmetic over `issued_at` only — no call to Kite.

- [x] G3: Acting is idempotent — running resync twice changes nothing the second time
  EVIDENCE: `services/worker/tests/test_resync_task.py::TestItIsIdempotent` against a real
  PostgreSQL/TimescaleDB (`10 passed`). `test_the_second_press_changes_nothing` presses twice over
  a seeded thin day and asserts `first.pending_at_start == 1`, `first.repaired` non-empty, then
  `second.pending_at_start == 0`, `second.repaired == []`, `second.queued == []`,
  `second.complete`, **and** `md5(string_agg(row::text))` over `ohlcv_daily` and `trading_day`
  identical either side of the second press — an updated row would move the checksum where a count
  would not (house rule 7). Idempotent by construction underneath: the bhavcopy re-ingest upserts
  on `(instrument_id, date)`, the calendar correction is an `ON CONFLICT DO UPDATE`, and the Kite
  pull verifies against Kite before storing.

- [x] G4: It is staff-gated, exactly like the two existing admin POSTs
  EVIDENCE: both routes hang off the same router, whose gate is declared once —
  `APIRouter(prefix="/admin", …, dependencies=[Depends(require_staff)])`
  (`routers/admin.py:71`). `services/api/tests/test_api_admin.py::TestEveryAdminRouteIsStaffGated`
  derives the route list **from the OpenAPI document**, so the two new operations were covered the
  moment they existed: anonymous → 401 on every one, signed-in non-staff → 404 (not 403; a 403 is
  an oracle, `docs/DECISIONS.md` §17.3). Named again in this leaf's own file so a reader can see
  it: `test_admin_resync.py::TestTheEndpoints::test_a_signed_in_non_staff_caller_gets_404_on_both`
  and `::test_the_dry_inspection_reports_what_is_pending_and_why`, which asserts 401 without a
  bearer before asserting 200 with one. `services/api/tests/test_api_artifacts.py` now documents
  `/admin/resync: {get, post}`, so the surface is written down rather than merely served.

- [x] G5: **It cannot place an order.** No execute path, no gateway call, no GTT.
  CHECK: grep -c "OrderGateway\|place_order\|place_gtt\|\.place(" decile-blueprint/services/api/src/baskfy_api/routers/admin.py
  EXPECT: 0
  EVIDENCE: the CHECK returns **0**. The same grep returns 0 for the other two modules this leaf
  owns: `services/api/src/baskfy_api/resync.py` → 0, and
  `services/worker/src/baskfy_worker/tasks/resync.py` → 0 (extended with `baskfy_execution`).
  Pinned as a test rather than left to a grep in a gate file:
  `test_admin_resync.py::TestItCannotPlaceAnOrder` reads all three sources and asserts none names
  `OrderGateway`, `place_order`, `place_gtt`, `baskfy_execution` or `packages.execution`, plus a
  second test that the three files exist so it cannot pass vacuously. The only broker contact in
  the whole path is `kite_session_cli.refresh_quietly()` — the M58 session bridge, which reads one
  string from the desk over a forced SSH command. Reviewed and admitted to the shared
  access-token-store allowlist as a **reader that never writes**
  (`services/api/tests/test_broker_oauth.py::test_every_module_touching_the_token_store_is_a_known_one`);
  `grep -c "\.save(" services/api/src/baskfy_api/resync.py` → 0.

- [x] G6: A button in the web app calls it and shows what happened — including "nothing was
      pending", which is a real and common answer
  EVIDENCE: `apps/web/src/components/admin/resync-panel.tsx`, rendered in a **Data sync** section
  at the top of `/admin` (`apps/web/src/app/(app)/admin/page.tsx`), streamed in behind
  `<Suspense>` so the deep check never blocks the rest of the staff page. Server actions
  `inspectResync` / `startResync` in `apps/web/src/app/actions/admin.ts` keep the staff bearer on
  the server. 11 tests in `apps/web/src/components/admin/__tests__/resync-panel.test.tsx`
  (`115 files / 2000 tests passed` for the whole web suite):

  * **nothing pending** → "Nothing is pending." plus "261 trading days checked, 2025-07-25 to
    2026-08-28". Asserted, because a panel that renders empty here is indistinguishable from one
    whose check failed.
  * **nothing pending but part of the check could not run** → the plain all-clear is asserted
    *absent*; the page says "Nothing pending in what could be checked" and lists the unresolved
    notes. "I could not look" must never render as "I looked and it was fine".
  * **work pending** → one row per finding with the date, the class in words, the numbers behind
    the verdict ("322 of ~2,310 bars") and the remedy that will run, before it runs. The button
    itself reads "Resync 2 pending items".
  * **partial repair** → "Did NOT close everything", with **Could not fix · 1**, **Not attempted
    yet · 1** and **Still pending · 1** as separate blocks, and the repaired line still credited.
  * **repair that worked** → "Closed everything it found."
  * **before any check has run** → "The data check has not run yet", never an all-clear.

  No spinner resolving to silence: `POST` answers 202, so the panel polls the inspection every 5 s
  for up to 3 minutes watching for a *new* `last_resync.completed_at`, and on timeout says the
  repair is still running rather than pretending it finished. Colour is never the sole carrier of
  meaning — every state carries its words (docs/11 §Accessibility).

- [x] G7: It reports what it could NOT fix, rather than reporting success for a partial repair
  EVIDENCE: `ResyncOutcome` has six first-class fields — `repaired`, `queued`, `failed`,
  `deferred`, `still_pending`, `unresolved` — and `complete` is `not (failed or deferred or
  still_pending)`. The report is produced by a **third inspection run after the repairs**, so
  "did it work" is measured rather than assumed. Four tests in
  `test_resync_task.py::TestItReportsWhatItCouldNotFix`:
  * a window with one fixable and one unfixable thin day → the fixable one is in `repaired` and
    reaches 180 bars, the unfixable one is in `failed` with "NSE published no bhavcopy for this
    date", is in `still_pending`, still holds its 25 bars, and `complete is False`;
  * an unreachable desk → `failed` carries the Kite line, the bar repairs still completed, and
    `complete is False` (a failed pull is a warning, never a stopped run — `kite_session_cli`);
  * a broker outage → `failed` carries "no task broker is reachable", `queued == []`, **and the
    day is still in `still_pending`** (queued-day exclusion is driven by days actually accepted by
    the broker, not by the first N candidates — otherwise an outage would hide behind an empty
    pending list);
  * every press writes its own report to `admin_action` (`data_resync_completed`), which is what
    the page reads back — asserted on actor, target and payload keys.
  The API renders it: `test_admin_resync.py::test_the_last_repairs_own_report_is_rendered_beside_the_plan`
  asserts `last_resync.complete is False`, the `failed` list and the `still_pending` list reach
  the response with the actor's email.

- [x] G8: full suite green, lint clean, no test weakened
  CHECK: cd decile-blueprint && timeout 1800 uv run pytest 2>&1 | tail -2
  EXPECT: /passed/
  EVIDENCE: `cd decile-blueprint && uv run pytest` →
  **`10 failed, 4419 passed, 3 skipped in 722.62s (0:12:02)`**.

  **Zero of those ten are mine, and the set is byte-identical to the baseline captured before the
  first line of this leaf was written** (`diff` of the two `FAILED` lists is empty once the two
  provider-doctor failures caused by sourcing `.env` into the baseline shell are removed). They
  are one long-standing drift, in a tree this leaf does not touch: the seeded universe list grew
  to 15 (`nse-sme-emerge`, M59) and the expected constants were never widened — `assert 15 == 14`,
  `assert 19 == 18`, `nse-sme-emerge has no members`. `services/api/tests/test_seed.py` says so in
  its own comment: *"this assertion has been red since 8f0f9be for every run that had a database
  to fail against."* The tenth is a Google sign-in allowlist test answering 401. Per this leaf's
  contract ("do not fix unrelated files"), they are reported rather than touched.

  This leaf adds **39 Python tests and 11 TypeScript tests, all green**:
  `services/api/tests/test_admin_resync.py` → `28 passed`;
  `services/worker/tests/test_resync_task.py` → `10 passed`;
  `services/worker/tests/test_celery_config.py` → `19 passed` (extended, see below);
  `apps/web` → `115 files / 2000 tests passed`, including the 11 in
  `resync-panel.test.tsx`.

  **Lint clean on everything touched.** `ruff check` and `ruff format --check` pass on all fifteen
  files. The only errors ruff reports in them are the **two pre-existing** `PLC0415` in
  `services/worker/src/baskfy_worker/tasks/celery_tasks.py:424,426` (function-level imports inside
  `compute_curated_metrics_task`, untouched). `mypy` (strict, 457 source files) reports **44
  errors, all pre-existing** — 50 before this leaf's own six were fixed, and none of the 44 names
  a file in this leaf. `pnpm --filter @baskfy/web exec tsc --noEmit` is clean; `eslint` reports
  only the pre-existing errors in `data-table.tsx`, `results-panel.tsx`, `basket/*` and
  `column-display.ts`, none in the files added here.

  **No test weakened.** Three existing tests were *extended*, each because the new surface must be
  covered by a check that already exists:
  * `test_celery_config.py` — `baskfy.ops.resync` added to the registered-tasks assertion and to
    the producer-routes-where-the-worker-listens map (`→ default`). A name the API publishes and
    the worker does not bind is a 202 the operator reads as "queued" while nothing ever runs.
  * `test_api_artifacts.py` — `/admin/resync: {get, post}` added to `EXPECTED_PATHS`. A route
    served and not written down is a surface nobody agreed to.
  * `test_broker_oauth.py::test_every_module_touching_the_token_store_is_a_known_one` — the
    detector admitted to the pinned allowlist **with the review the test exists to force**: it is
    a reader that never writes (`grep -c "\.save(" resync.py` → 0), reads only `issued_at`, never
    returns or logs the token, and the repair it triggers goes through the M58 bridge.

  Judgement calls are recorded in `docs/DECISIONS-MERGE.md` under "Leaf 3.1 — the resync button
  ⚠ UNREVIEWED", per the autonomy charter.
