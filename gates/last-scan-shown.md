# gates/last-scan-shown.md — every strategy says when it last scanned (Agent L)

**What Maulik reported (12 Sep 2026).** *"None of the scan shows when the last scan performed in
any strategy, we need to show that as well."*

**Why nothing showed, and it was three different reasons wearing the same clothes.**

1. **`/vbt/today` did not carry the run.** The web's `fetchLastScan` accepts either an inlined
   `last_scan` or a `last_scan_id` to poll, and the payload carried neither, so a cold load had no
   run to ask about. Deliberate when it was written — the contract fixes the poll route, not the
   payload — and wrong in practice: the line could only appear after somebody pressed the button
   *in that same tab*.
2. **`/twt/today` did not exist at all** when this work started; a concurrent agent landed it
   mid-session, inlining the run. So this leaf builds on that rather than adding a second route.
3. **The copy said nothing worth reading even when the run arrived.** `scanRunLine` answered the
   empty string until a run existed — so a strategy whose rows came from the nightly looked as
   though it had never been scanned — and "Last scan finished 12 Sept 2026, 16:59 IST" otherwise,
   which is a stamp the reader has to subtract from their own clock and says nothing about what
   the scan *found*. A finished run that flagged fifty names and one that flagged none rendered
   identically.

**What a reader gets now**, on all three pages, from one shape of sentence:

| State | `/swing` | `/vbt` and `/twt` |
|---|---|---|
| never scanned, nothing published | "No scan has run yet." | same |
| never scanned, a session published | "No scan has been started from here yet — what is shown is the nightly run's, for the 11 Sept 2026 session." | same |
| queued | "Scan queued. This page updates when it finishes." | same |
| running | "Scanning now. This page updates when it finishes." | same |
| finished, found names | "Last scanned 12 minutes ago from published bars — 41 liquid, 2 setups." | "Last scanned 12 minutes ago — 3 signals." |
| finished, found none | "…— 41 liquid, no setups, which is an ordinary day." | "…— no signals, which is the ordinary result here." (`/twt`) / "…which is an ordinary day when the tape is quiet." (`/vbt`) |
| finished, run did not say | "Last scanned 12 minutes ago from published bars." | "Last scanned 12 minutes ago." |
| older than a day | "Last scanned at 12 Sept 2026, 16:59 IST …" | same |
| failed | "The last scan did not finish, so nothing changed. It stopped 12 minutes ago. You can start another." | same |

**The two rules the sentences keep.** No status code, no raw enum, no table, column, job or
setting name — asserted by a census over every state. And **no worker error string**: `/vbt` and
`/twt` already refused to render `run.error`; `/swing` rendered it verbatim and had a test pinning
that, which is the defect of 11 Sep 2026 said again. The test now asserts the opposite.

**Zero is an answer, not a gap.** `found` is `int | None` end to end: `0` means the detector looked
and found none — the ordinary result for TWT (about eighteen entries a year) and a legitimate one
for VBT on a thin tape — and `None` means the run did not say. A single field defaulted to zero
would have made a quiet week read as a broken job.

**Why the time is passed in rather than read.** These controls are client components under server
pages. A `Date.now()` inside render is taken twice — once on the server, once as the browser
hydrates — and disagrees at a bucket boundary, which React reports as a text mismatch. `null` is
the server's reading and yields the IST stamp; `useTickingNow` supplies the browser's afterwards,
and the age then ticks while somebody watches it.

**Scope kept.** Read-only against the box, nothing deployed, no execution flag and no sleeve
capital touched — `POST /twt/scan` / `POST /vbt/scan` are unchanged and their money-free tests
still run here (L10). `apps/web/src/lib/twt/{fetch,view}.ts` were **not** edited (Agent R owns
them this session); the richer fields are declared in each sleeve's own `copy.ts` as an optional
widening, so the transport type never had to change.

Run from the repo root. `BASKFY_TEST_DATABASE_URL` points at `baskfy_agentL`, its own database,
because three other agents were running concurrently.

⚠️ **Run this ledger serially, and not while another pytest is pointed at `baskfy_agentL`.** Two
re-runs on 12 Sep showed transient reds that were nothing to do with the code: L10 went red when a
second pytest raced it on the same database, and L9's `tsc` went red for one run on
`Type 'LayoutRoutes' is not assignable to type '"/"'` while a concurrent agent was regenerating
Next's typed routes. Both came back green the moment they were run alone. A red here is worth
re-running once before it is worth investigating.

---

## The ledger

- [x] L1: **Every sleeve's day payload names this user's newest run.** `/swing/setups` always did;
      `/vbt/today` and `/twt/today` now do, and answer an explicit `null` before the first scan
      rather than omitting the field.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentL .venv/bin/pytest services/api/tests/test_api_vbt_scan.py services/api/tests/test_api_twt_scan.py -k "the_day_s_payload_names_the_newest_run or the_run_on_the_day_s_payload_is_this_user_s_own" 2>&1 | tail -3
  EXPECT: /^4 passed(, \d+ deselected)? in /m
  EVIDENCE: `4 passed, 26 deselected`. Two sleeves × (the payload names the run · somebody else's
  press never dates this reader's page).

- [x] L2: **The run says what it found, and "none" is not "did not say".** `found` is lifted out of
      the worker's own detail — `{"signals": n, …}` flattened for VBT, nested under `detail` for
      TWT — and `0` and `None` survive the payload as different answers.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentL .venv/bin/pytest services/api/tests/test_api_vbt_scan.py services/api/tests/test_api_twt_scan.py -k "found" 2>&1 | tail -3
  EXPECT: /^4 passed(, \d+ deselected)? in /m
  EVIDENCE: `4 passed`. A DONE run with three signals reads `found == 3`; a quiet one reads `0`; a
  FAILED one and a run whose detail never carried the key both read `None`.

- [x] L3: **The line says WHEN, in the words a person uses — and degrades to the IST wall clock
      past a day rather than saying "17,412 minutes ago".** Also the hydration rule: no clock, no
      age, so the server's pass is the stamp and cannot mismatch.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/lib/__tests__/scan-age.test.ts 2>&1 | tail -6
  EXPECT: /^ *Tests +\d+ passed \(\d+\)$/m
  EVIDENCE: 12 passed — "just now", "a minute ago", "12 minutes ago", "an hour ago", "5 hours ago",
  then `null` past the 24-hour horizon so the caller falls back to `at 12 Sept 2026, 16:59 IST`.
  A stamp a few seconds in the future reads "just now" rather than a negative age.

- [x] L4: **Never scanned says so — and names the nightly run when a session IS published.** This
      is the half of the gap that mattered most: the strategies had all been scanned overnight, and
      the page said nothing.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run "src/app/(app)/swing/__tests__/scan-run-line.test.ts" "src/app/(app)/vbt/__tests__/scan-now.test.tsx" "src/app/(app)/twt/__tests__/scan-now.test.tsx" -t "nightly" 2>&1 | tail -6
  EXPECT: /^ *Tests +3 passed \|/m
  EVIDENCE: three sleeves, one sentence: "No scan has been started from here yet — what is shown is
  the nightly run's, for the 11 Sept 2026 session." With no published session it is "No scan has
  run yet." — two states, two sentences, neither of them the empty string.

- [x] L5: **A scan in flight says so, and the page says it will keep up.** Queued and running are
      distinct sentences, the button is disabled in both, and the poll still stops on DONE.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run "src/app/(app)/vbt/__tests__/scan-now.test.tsx" "src/app/(app)/twt/__tests__/scan-now.test.tsx" "src/app/(app)/swing/__tests__/scan-run-line.test.ts" -t "queued" 2>&1 | tail -6
  EXPECT: /^ *Tests +\d+ passed \|/m
  EVIDENCE: "Scan queued. This page updates when it finishes." / "Scanning now. This page updates
  when it finishes." The polling tests either side of them are untouched and still green (L9).

- [x] L6: **A run that found nothing reads as an ordinary result, not a fault.** The sentence says
      so in words, and the live region keeps the muted token rather than the negative one.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run "src/app/(app)/vbt/__tests__/scan-now.test.tsx" "src/app/(app)/twt/__tests__/scan-now.test.tsx" "src/app/(app)/swing/__tests__/scan-run-line.test.ts" -t "found nothing" 2>&1 | tail -6
  EXPECT: /^ *Tests +3 passed \|/m
  EVIDENCE: three sleeves assert the zero sentence, that it matches none of `/fail|error|wrong|
  problem/i`, and (on the two rendered ones) that the status region is `text-muted-foreground`.

- [x] L7: **A failed run says what a person can act on, and never the worker's own sentence.**
      `/swing` leaked `run.error` verbatim onto a customer-facing page **and had a test named
      "shows the reason when the last scan failed" pinning it**. That test now asserts the reverse.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run "src/app/(app)/swing/__tests__/page.test.tsx" "src/app/(app)/swing/__tests__/scan-run-line.test.ts" "src/app/(app)/vbt/__tests__/scan-now.test.tsx" "src/app/(app)/twt/__tests__/scan-now.test.tsx" -t "did not finish" 2>&1 | tail -6
  EXPECT: /^ *Tests +4 passed \|/m
  EVIDENCE: all four assert the three facts that are the reader's — when it stopped, that nothing
  changed, that they may start another — and that "ScanNotRunnable", "ohlcv_daily" and "Kite" are
  absent. `grep -c 'run.error' app/(app)/swing/copy.ts` is now 0.

- [x] L8: **Nothing from the inside of the system reaches the reader, in any state.** The census
      that found the 11 Sep defect, re-run over every state of the control **and** over both
      spellings of it — with a published session behind it and without.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run "src/app/(app)/vbt/__tests__/scan-now.test.tsx" "src/app/(app)/twt/__tests__/scan-now.test.tsx" "src/app/(app)/swing/__tests__/scan-run-line.test.ts" -t "internal|names no route" 2>&1 | tail -6
  EXPECT: /^ *Tests +\d+ passed \|/m
  EVIDENCE: no route, host, source file, snake_case field, SHOUTING_SETTING — and, on the swing
  suite, no raw `DONE|FAILED|QUEUED|RUNNING` either. Six run states × two session states.

- [x] L9: **The web suites for all three sleeves are green, `tsc` is clean, and `eslint` reports
      zero errors on everything this leaf touched.**
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run "src/app/(app)/swing" "src/app/(app)/vbt" "src/app/(app)/twt" src/lib/__tests__/scan-age.test.ts 2>&1 | tail -6 && pnpm exec tsc --noEmit && echo "TSC-CLEAN" && pnpm exec eslint src/lib/scan-age.ts src/lib/use-ticking-now.ts src/lib/__tests__/scan-age.test.ts "src/app/(app)/swing" "src/app/(app)/vbt" "src/app/(app)/twt" 2>&1 | tail -3 && echo "ESLINT-CLEAN"
  EXPECT: /^ *Tests +\d+ passed \(\d+\)$[\s\S]*TSC-CLEAN[\s\S]*ESLINT-CLEAN/m
  EVIDENCE: 14 files / 234 tests passed, `tsc --noEmit` silent, eslint silent over the touched
  paths. ⚠️ A full `pnpm exec vitest run` and a full `eslint .` show **3 failures and 3 errors from
  `src/lib/swing/__tests__/read-contract.test.ts`**, an untracked file belonging to a concurrent
  agent. It exercises `lib/swing/fetch.ts`, which this leaf did not touch; not mine to fix, and
  deliberately not scoped into this check rather than silently counted as green.

- [x] L10: **The API is green, typed and linted — and the scan is still money-free.** The
      money-free assertions (`a press writes the scan row and nothing else`, `a press does not
      touch the sleeve's capital`) run in the same suites, so the new fields could not have been
      bought with a widened write.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentL .venv/bin/pytest services/api/tests/test_api_vbt_scan.py services/api/tests/test_api_twt_scan.py services/api/tests/test_vbt_readonly.py services/api/tests/test_twt_readonly.py services/api/tests/test_api_swing.py services/api/tests/test_api_vbt.py services/api/tests/test_api_twt_today.py 2>&1 | tail -3 && python3 -c "import json;d=json.load(open('packages/api-client/openapi.json'));s=d['components']['schemas'];print('ARTIFACTS', 'last_scan' in s['VbtTodayOut']['properties'], 'found' in s['VbtScanRunOut']['properties'], 'found' in s['TwtLastScanOut']['properties'], 'found' in s['TwtScanRunOut']['properties'])" && .venv/bin/ruff check services/api/src/baskfy_api/routers/vbt.py services/api/src/baskfy_api/routers/twt.py services/api/src/baskfy_api/vbt_scan.py services/api/src/baskfy_api/twt_scan.py && .venv/bin/mypy services/api/src/baskfy_api/routers/vbt.py services/api/src/baskfy_api/routers/twt.py services/api/src/baskfy_api/vbt_scan.py services/api/src/baskfy_api/twt_scan.py 2>&1 | tail -2
  EXPECT: /^\d+ passed in [\d.]+s$[\s\S]*^ARTIFACTS True True True True$[\s\S]*All checks passed![\s\S]*Success: no issues found in 4 source files/m
  EVIDENCE: the whole sleeve API surface green, the four new response fields present in the
  checked-in `packages/api-client/openapi.json`, ruff clean, mypy clean.
  ⚠️ `test_api_artifacts.py` is **not** in this list, and the reason is not convenience. It
  compares the checked-in OpenAPI document byte-for-byte against a fresh render, and at 17:35 on
  12 Sep a concurrent agent added `GET /twt/backtest` to the router without regenerating — so it
  fails on a path this leaf never touched. This leaf's own fields **are** regenerated and present,
  which is what the `ARTIFACTS` line asserts directly instead of borrowing a red test's verdict.
  Whoever lands `/twt/backtest` runs `make openapi && make client`.

- [x] L11: **The `/twt` and `/vbt` read surfaces stayed read-only, and no second route was added
      to reach the run.** The scan is still the one non-GET on each; `/twt/scan/latest` was drafted
      and then **deleted** when the concurrent `/twt/today` landed carrying the run inline.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_agentL .venv/bin/pytest services/api/tests/test_twt_readonly.py services/api/tests/test_vbt_readonly.py -k "read_only or every_route_is_a_get or mutating" 2>&1 | tail -3; ls services/api/src/baskfy_api/routers/ | grep -c "twt.py"; grep -c "scan/latest" services/api/src/baskfy_api/routers/twt.py; true
  EXPECT: /^\d+ passed(, \d+ deselected)? in [\d.]+s$[\s\S]*^1$[\s\S]*^0$/m
  EVIDENCE: the read-only censuses pass, one `twt.py`, zero mentions of a `scan/latest` route. The
  web helper `src/lib/twt/last-scan.ts` was removed with it, so `lib/twt/fetch.ts` (Agent R's this
  session) was never edited and `/twt`'s page still reads through `fetchLastScan`.
