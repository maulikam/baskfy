# The scan ran, the page said nothing had been read — the reader did not exist

**Agent R (read path). 12 Sep 2026.** Read-only against the box; no deploy, no write to
production. Scope: `decile-blueprint/`.

---

## The bug, and the two explanations that were wrong

Maulik pressed **"Scan now"** on `/twt`. The scan **ran and succeeded**. The page still said:

> Nothing has been read for this strategy yet.

Measured on the box before anything was changed — `tw_scan_run` ids 1 and 2 (and a third at
17:02), all **DONE**, no error, ~19 s each, all `source=web`, all `session_date=2026-09-11`, all
`user_id=1`. The detector wrote, for **user 1**, session **2026-09-11**: one `tw_breadth_daily`
row (`OPEN`, `pct_above_dma` 51.1588 of 1,769 measured, 905 above), **2** `tw_signal_daily` rows
(IOLCP, OPTIEMUS, both `SIGNAL`) and **58** `tw_state_daily` rows. An independent local run of the
same detector for the same session produced 57 names (`gates/twt-chartink-gap.md`). The data was
right.

**Suspect 1 — a date mismatch.** The scan route's own docstring says the worker detects "the
latest **published** session — the pipeline's date". If the reader asked for *today* (12 Sep, a
Saturday) or for the newest `pipeline_run`, it would find nothing and render exactly this. **Not
it.** There was no reader to ask for a date.

**Suspect 2 — a user mismatch.** `BASKFY_SOLE_USER_ID` is absent from the box's
`.env.staging.compose`; portfolios exist for users 1 and 6; no `app_user` row carries
`E2E_PUBLIC_ID`, which is `resolve_sole_user_id`'s fallback. A reader defaulting elsewhere would
render exactly this too. **Not it.** `docker compose exec api env | grep -i sole` on the box
returns `BASKFY_SOLE_USER_ID=1` — `compose.prod.yml` sets it whatever the env file says — and the
writer and the reader call the *same* `scoped_sole_user_id`, so they cannot disagree.

## The actual cause, in three sentences

`apps/web/src/lib/twt/fetch.ts` has asked for `GET /api/v1/twt/today` since TW8, and **no such
route was ever built** — `create_app().openapi()` on the box returns exactly
`['/api/v1/twt/scan', '/api/v1/twt/scan/{run_id}']`. `readOrNull` turns that 404 into `null` **by
design**, so a page whose job has not run yet renders an empty state instead of a 500 — and `null`
is also precisely what an empty database produces, so the two are indistinguishable at the
component that prints the sentence. The scan was never the bug: the writer wrote, and the reader
had never been built.

## The fix

`services/api/src/baskfy_api/twt.py` (a read-only service) and one `@router.get("/today")` on
`services/api/src/baskfy_api/routers/twt.py`. The session it resolves is **the latest
`tw_breadth_daily` row the detector wrote** — the one resolution that cannot drift ahead of the
writer, since "the latest published session" is what the worker detected. `?date=` overrides it;
a named date with no reading answers an empty view *stamped with that date*. Decisions:
`docs/twt/DECISIONS-TW.md` **TW13.1–TW13.4**.

---

## Ledger

- [x] R1: **The blindness, at the commit it was found on.** `routers/twt.py` declared two routes
      and neither was `/today`, while `fetch.ts` was already asking for `/twt/today`. Pinned to
      `95fd001` so this row keeps saying what was true, not what is true now.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && git show 95fd001:decile-blueprint/services/api/src/baskfy_api/routers/twt.py | grep -c '^@router\.'; git show 95fd001:decile-blueprint/services/api/src/baskfy_api/routers/twt.py | grep -c 'router.get("/today"'; git show 95fd001:decile-blueprint/apps/web/src/lib/twt/fetch.ts | grep -c '"/twt/today"'
  EXPECT: /^2\n0\n1$/m
  EVIDENCE: `2`, `0`, `1` — two route decorators, no `/today` among them, one page asking for it. The box agreed: `create_app().openapi()` in the running API container listed `['/api/v1/twt/scan', '/api/v1/twt/scan/{run_id}']` and, for contrast, eight `/vbt` paths including `/api/v1/vbt/today`.

- [x] R2: **The route exists now, and it is a GET.** Non-vacuity for every row below: they all
      pass trivially against an unregistered route, which is the state this surface was in.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && .venv/bin/python -c "from baskfy_api.app import create_app; s=create_app().openapi(); p=s['paths'].get('/api/v1/twt/today'); print('verbs', sorted(p) if p else 'ABSENT')"
  EXPECT: /^verbs \['get'\]$/m
  EVIDENCE: `verbs ['get']`. The full `/twt` surface is now `['/api/v1/twt/scan', '/api/v1/twt/scan/{run_id}', '/api/v1/twt/today']`.

- [x] R3: **The reader resolves the session the writer wrote, not today.** Asserted in the
      situation the bug was found in: the calendar day is 12 Sep, the only detected session is
      11 Sep, nothing is asked for by date. A reader on "today" or on the newest `pipeline_run`
      answers an empty page here.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentR .venv/bin/python -m pytest "services/api/tests/test_api_twt_today.py::TestTheSessionTheReaderResolves::test_a_scan_that_wrote_yesterday_is_read_today" "services/api/tests/test_api_twt_today.py::TestTheSessionTheReaderResolves::test_the_page_has_something_to_say" --no-header -p no:randomly 2>&1 | tail -3
  EXPECT: /^2 passed/m
  EVIDENCE: `2 passed`. `as_of` is `2026-09-11`, `gate.gate` is `OPEN`, and `gate.gate` is not null — which is the negation of the sentence the page printed.

- [x] R4: **A named session with no reading is stamped with the date asked for**, and a database
      with nothing in it answers `as_of: null` with a 200 — not a 404, which would be
      indistinguishable from the bug this route fixes.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentR .venv/bin/python -m pytest "services/api/tests/test_api_twt_today.py::TestTheSessionTheReaderResolves::test_a_named_session_with_no_reading_is_stamped_with_the_date_asked_for" "services/api/tests/test_api_twt_today.py::TestTheSessionTheReaderResolves::test_nothing_detected_at_all_is_a_null_as_of_and_not_an_error" --no-header -p no:randomly 2>&1 | tail -3
  EXPECT: /^2 passed/m
  EVIDENCE: `2 passed`. `?date=2026-09-04` answers `as_of: "2026-09-04"` with a null gate; an empty sleeve answers `as_of: null` and still serves `threshold_pct`.

- [x] R5: **A name in the state with no entry event is served.** 56 of the box's 58 rows were in
      exactly that state; an inner join onto `tw_signal_daily` would have served two names out of
      fifty-eight and looked correct.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentR .venv/bin/python -m pytest "services/api/tests/test_api_twt_today.py::TestTheNamesInTheState::test_a_name_with_no_entry_event_is_served" "services/api/tests/test_api_twt_today.py::TestTheNamesInTheState::test_the_entries_and_the_reject_carry_their_state" --no-header -p no:randomly 2>&1 | tail -3
  EXPECT: /^2 passed/m
  EVIDENCE: `2 passed`. `QUIETONE` comes back with `signal_state: null`; the two entries come back `SIGNAL`; the reject comes back `SCAN_ONLY` with `failed_filters: ["TURNOVER"]`, because `05` §1.2 shows what it passed over.

- [x] R6: **The funnel keeps "the screened market" and "the names with a bar" apart.**
      `tw_breadth_daily.universe_count` is *already* the count with a bar; the wider number lives
      only in `detail`. Serving the column as both would have understated the market threefold
      and still looked plausible.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentR .venv/bin/python -m pytest "services/api/tests/test_api_twt_today.py::TestTheGateAndItsFunnel" --no-header -p no:randomly 2>&1 | tail -3
  EXPECT: /^2 passed/m
  EVIDENCE: `2 passed`. The box's own funnel — `{"instruments": 10155, "with_a_bar": 3413, "with_a_dma": 1769, "above_the_dma": 905, "in_state": 58, "signals": 2}` — is the fixture, and each step is served as a strict subset of the one above. A row with no `detail` serves nulls, never zeroes: the page drops a null step and cannot drop a zero.

- [x] R7: **Money arrives quoted, at the precision it was stored at.** `close_raw` is `PRICE_RAW`'s
      four decimals, `week_close_0` is `PRICE`'s two, and neither is trimmed. `JSON.parse` on the
      bare token `149.6000` is the double `149.6`, and `@/lib/twt/numbers` does `BigInt`
      arithmetic over decimal strings precisely so a rupee never passes through a float
      (house rules 8 and 9; DECISIONS-TW TW13.3).
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentR .venv/bin/python -m pytest "services/api/tests/test_api_twt_today.py::TestTheNamesInTheState::test_prices_keep_the_precision_they_were_stored_at" --no-header -p no:randomly 2>&1 | tail -3
  EXPECT: /^1 passed/m
  EVIDENCE: `1 passed`. The payload carries `"close_raw":"149.6000"`, `"week_close_0":"149.60"`, `"pct_above_dma":"51.1588"`, and no bare `149.6` anywhere.

- [x] R8: **The half-size counter and the book ride along**, so the hub is one request and the
      counter never ticks in the dark.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentR .venv/bin/python -m pytest "services/api/tests/test_api_twt_today.py::TestTheRestOfThePayload" --no-header -p no:randomly 2>&1 | tail -3
  EXPECT: /^3 passed/m
  EVIDENCE: `3 passed`. `half_size` is `{"entries_left": 10, "entries_total": 10, "execution_enabled": false}`; the book is `[]` and not null; the newest `tw_scan_run` is inlined as `last_scan` with `id` (the field `fetchLastScan` reads) and carries no `provisional` — TW12.2.

- [x] R9: **The tenant was never the bug.** Both halves resolve the same user through the same
      function, and on the box every row the writer wrote and every scan run sits under `user_id`
      **1**. Read-only SQL; no write to production.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=baskfy-poc BOX_SQL_LINES=10 bash tools/deploy/box-sql.sh "select 'scan', user_id, count(*) from tw_scan_run group by 2 union all select 'signal', user_id, count(*) from tw_signal_daily group by 2 union all select 'state', user_id, count(*) from tw_state_daily group by 2 order by 1"
  EXPECT: /^signal\|1\|2$/m
  EVIDENCE: `scan|1|3`, `signal|1|2`, `state|1|58` — one tenant on both sides of the seam. The API container's environment carries `BASKFY_SOLE_USER_ID=1` (from `compose.prod.yml`) even though `.env.staging.compose` never mentions it, which is why `resolve_sole_user_id`'s `E2E_PUBLIC_ID` fallback — for which the box has no `app_user` row — is never reached.

- [x] R10: **Every `/twt` route resolves the sole tenant rather than trusting the principal**, and
      the new one refuses a stranger with a 404 rather than serving somebody else's book.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && grep -c 'await scoped_sole_user_id(' services/api/src/baskfy_api/routers/twt.py && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentR .venv/bin/python -m pytest "services/api/tests/test_api_twt_today.py::TestItIsOnePersonsBook" --no-header -p no:randomly 2>&1 | tail -3
  EXPECT: /^2 passed/m
  EVIDENCE: three calls for three routes; a foreign principal gets 404 (M43.4 — refused, not silently promoted) and an anonymous caller 401.

- [x] R11: **A read writes nothing.** The claim that matters most on this sleeve, asserted after a
      request rather than reasoned about: the capital is untouched, the first-live countdown has
      not moved, and no scan was queued.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentR .venv/bin/python -m pytest "services/api/tests/test_api_twt_today.py::TestItWritesNothing" --no-header -p no:randomly 2>&1 | tail -3
  EXPECT: /^1 passed/m
  EVIDENCE: `1 passed`. `sleeve_capital_inr` still `1000000.00`, `first_live_entries_left` still 10, `tw_scan_run` still empty.

- [x] R12: **The surface is still read-only except the scan, and the census now reads the new
      route.** `test_twt_readonly.py`'s path count was an exact equality at two, so serving
      `/twt/today` turned it red before a line of its own test existed — the count working, not
      failing. The capital rail, the execution-package rail and the auto-execute rail now cover
      the new read module too.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentR .venv/bin/python -m pytest services/api/tests/test_twt_readonly.py services/api/tests/test_api_artifacts.py --no-header -p no:randomly 2>&1 | tail -3
  EXPECT: /^27 passed/m
  EVIDENCE: `27 passed`. One `@router.post(` on the surface, and it is the scan; `openapi.json` and the generated TypeScript client regenerated (`make openapi`, `make client`) so `test_openapi_json_is_current` and `test_it_was_generated_from_this_document` hold.

- [x] R13: **The flag is reported, never consulted.** `05` §1.4's counter must say whether trading
      is switched off, so `routers/twt.py` names `twt_execution_enabled` exactly once — as a
      keyword handed to the read view — and neither `twt_scan` nor the read service names it at
      all. No branch on it anywhere (DECISIONS-TW TW13.2).
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && grep -c 'twt_execution_enabled' services/api/src/baskfy_api/routers/twt.py services/api/src/baskfy_api/twt.py services/api/src/baskfy_api/twt_scan.py; grep -c 'if settings.twt_execution_enabled' services/api/src/baskfy_api/routers/twt.py
  EXPECT: /routers\/twt\.py:1\nservices\/api\/src\/baskfy_api\/twt\.py:0\nservices\/api\/src\/baskfy_api\/twt_scan\.py:0\n0$/m
  EVIDENCE: `1`, `0`, `0`, and zero branches. `POST /twt/execute` still does not exist and `BASKFY_TWT_EXECUTION_ENABLED` was not set, read into a decision, or defaulted true.

- [x] R14: **The page is still read-only and still renders.** `read-only.test.tsx` is the census
      that keeps the set of server actions under `/twt` at exactly one — "Scan now" — and no
      second write was added for this fix.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/apps/web 2>/dev/null || cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web; pnpm vitest run "src/app/(app)/twt/__tests__/read-only.test.tsx" "src/app/(app)/twt/__tests__/page.test.tsx" 2>&1 | tail -5
  EXPECT: /Tests  26 passed \(26\)/m
  EVIDENCE: `Tests 26 passed (26)` — 15 read-only assertions and 11 page assertions. The page's one server action is unchanged; `TwtWritePath` is still the single string `"/twt/scan"`.

- [x] R15: **The page no longer claims a live mark.** `services/api` has no quote path at all, so
      an open line is marked at the session's published close. Root `CLAUDE.md`'s "Which date the
      product shows" section is the precedent: a document that misstated where a money figure
      came from was believed, and the owner had to correct it (DECISIONS-TW TW13.4).
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && grep -c 'marked at the live price' "apps/web/src/app/(app)/twt/page.tsx"; grep -c "not at a live price" "apps/web/src/app/(app)/twt/page.tsx"; grep -rn 'get_ltp(\|get_quotes(' services/api/src | wc -l | tr -d ' '
  EXPECT: /^0\n1\n0$/m
  EVIDENCE: `0`, `1`, `0`. The footnote reads "Open positions are marked at that session's close, not at a live price", and the whole of `services/api/src` calls no quote function — the only `get_ltp` in the repository is the desk's, in `kite-momentum-rebalancer/app/`. `last_price` is null when nothing has printed since the fill: a reason, not a zero.

- [x] R16: **The whole read suite, end to end**, including the seam test another agent wrote
      independently against the same symptom before this fix existed.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentR .venv/bin/python -m pytest services/api/tests/test_api_twt_today.py services/api/tests/test_twt_scan_to_page.py --no-header -p no:randomly 2>&1 | tail -3
  EXPECT: /^19 passed/m
  EVIDENCE: `19 passed` — 15 of this agent's and 4 of `test_twt_scan_to_page.py`'s, which was written from the same production symptom by a different agent and never saw this implementation. A fix that satisfies a test it was not written against is the closest thing available to an independent confirmation.

---

## What is deliberately NOT in this fix

* **`/twt/backtest` is still unserved and still answers `null`.** That page's rows genuinely do
  not exist yet, so its empty state is still true. This router does not grow a surface on
  speculation twice (TW13.1, rejected (d)).
* **Nothing was deployed.** The box still serves two `/twt` paths; every measurement above that
  touches it is a `SELECT` through `tools/deploy/box-sql.sh`. Putting this on the box is a
  deploy, and a deploy is somebody's decision, not an agent's.
* **`tw_config.sleeve_capital_inr` was not set and `BASKFY_TWT_EXECUTION_ENABLED` was not
  touched.** Both are named in the root safety rails; R13 asserts the second structurally.
* **`readOrNull` still cannot tell a 404 from an empty payload.** It is the mechanism that made a
  missing route look like a quiet strategy, and it is worth fixing — but fixing the message is not
  fixing the hole, and the hole is fixed. Recorded as TW13.1 rejected (c).

## Known-red neighbours, and why they are not this agent's

At the time of writing, `src/app/(app)/twt/__tests__/scan-now.test.tsx` has four failing tests and
`test_api_twt_scan.py::test_the_answer_has_no_provisional_field` one, because another agent is
adding a `found` field and a last-run line to the scan surface in the same working tree. Both
fail identically with this agent's changes stashed, and neither touches the read path. R14 runs
`read-only.test.tsx` and `page.test.tsx` rather than the whole directory for that reason, and
says so rather than quietly selecting green files.
