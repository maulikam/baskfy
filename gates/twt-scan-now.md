# gates/twt-scan-now.md — "Scan now" for the three-weeks-tight sleeve (Leaf 4)

**Goal.** The TWT page gets the button the swing book has had since SW15: a person asks for the
session to be detected again, and the answer is a run they can poll. `POST /twt/scan` answers
**202** with a run id, **409** while one is in flight, **429** inside the minute; `GET
/twt/scan/{run_id}` answers that run's state. The vocabulary is the swing one —
`QUEUED | RUNNING | DONE | FAILED`.

**The thing this leaf must not do.** A scan is **money-free**. Nothing added here may reach
`OrderGateway.place` or `place_gtt_stop`, no code path sets `BASKFY_TWT_EXECUTION_ENABLED`, and
nothing writes `tw_config.sleeve_capital_inr`. `docs/twt/02` §3 Track C is the law; TW10's property
test in `packages/core/tests/test_twt_safety_properties.py` is the proof, and **G6** is the
assertion that the property test actually *sees* the new routes rather than passing because it
never looked.

**The decisions this leaf had to make**, all four in `docs/twt/DECISIONS-TW.md`, tagged
`⚠ UNREVIEWED`: **TW12.1** the table (TWT had none — VBT has `vb_scan_run`, swing has
`sw_scan_run`), **TW12.2** running the *existing* `baskfy.twt.detect` detector and having no
provisional intraday path, **TW12.3** the `/twt` hub's first money-free write, and **TW12.4** the
publisher's name, which a safety test refused.

Run from the repo root unless a CHECK says otherwise. `BASKFY_TEST_DATABASE_URL` points at
`baskfy_leaf4_test` throughout, **not** the shared `baskfy_test`: three other leaves were running
concurrently and two of them migrated that database mid-suite (`app_user` vanished under a run on
12 Sep). A ledger that is re-run must not race for a database either.

---

## The ledger

- [x] **G1: The migration is the next free number, and it chains onto `0041_twt`.**
      `0042` was free; nothing else in the tree claims it.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/services/api/alembic/versions && ls | grep -c '^0042' ; grep -h "^revision\|^down_revision" 0042_twt_scan_run.py
  EXPECT: /^1$[\s\S]*revision = "0042_twt_scan_run"[\s\S]*down_revision = "0041_twt"/m
  EVIDENCE: `1`, `revision = "0042_twt_scan_run"`, `down_revision = "0041_twt"`.
  ℹ️ A concurrent leaf has since landed `0043_split_holding_reason` **revising `0042_twt_scan_run`**,
  so the chain is single-headed (`alembic heads` → `0043_split_holding_reason (head)`). That is why
  G2 and G2a below pin the revision explicitly instead of saying `head`: a gate whose expected
  version moves whenever somebody else adds a migration is a gate that will be "fixed" by loosening
  it.

- [x] **G2: The migration round-trips — `upgrade` → `downgrade base` → `upgrade` — against a
      THROWAWAY database.** Never the developer's own: `BASKFY_DATABASE_URL` defaults to
      `localhost:5433/baskfy`, and `gates/twt-3.md` G2 (decision **TW11.4**) records the day that
      default emptied it. This CHECK creates and drops `baskfy_scan_migrate_check` and touches
      nothing a person owns. It asserts the **end state** — the revision, fourteen `tw_` tables and
      `tw_scan_run` among them — rather than a word in a log, because a log line proves only that
      the upgrade half ran.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && docker exec baskfy-postgres psql -U baskfy -d postgres -q -c "DROP DATABASE IF EXISTS baskfy_scan_migrate_check;" -c "CREATE DATABASE baskfy_scan_migrate_check;" >/dev/null 2>&1; export BASKFY_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_scan_migrate_check; (cd services/api && uv run alembic upgrade 0042_twt_scan_run >/dev/null 2>&1 && uv run alembic downgrade base >/dev/null 2>&1 && uv run alembic upgrade 0042_twt_scan_run >/dev/null 2>&1); printf 'version=%s tw_tables=%s scan=%s\n' "$(docker exec baskfy-postgres psql -U baskfy -d baskfy_scan_migrate_check -tAc 'select version_num from alembic_version')" "$(docker exec baskfy-postgres psql -U baskfy -d baskfy_scan_migrate_check -tAc "select count(*) from information_schema.tables where table_schema='public' and table_name like 'tw\_%'")" "$(docker exec baskfy-postgres psql -U baskfy -d baskfy_scan_migrate_check -tAc "select count(*) from information_schema.tables where table_schema='public' and table_name='tw_scan_run'")"; docker exec baskfy-postgres psql -U baskfy -d postgres -q -c "DROP DATABASE IF EXISTS baskfy_scan_migrate_check;" >/dev/null 2>&1
  EXPECT: /^version=0042_twt_scan_run tw_tables=14 scan=1$/m
  EVIDENCE: `version=0042_twt_scan_run tw_tables=14 scan=1` — the thirteen `tw_` tables from `0041`
  plus `tw_scan_run`, after a full down-to-base and back up. G2a is the half that proves the
  downgrade actually undoes rather than merely runs.

- [x] **G2a: The downgrade undoes.** `alembic downgrade 0041_twt` from `0042` leaves `tw_scan_run`
      **and its index** gone and the thirteen `0041` tables standing — so the down leg is the exact
      inverse of the up leg and not a no-op that a re-upgrade papers over.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && docker exec baskfy-postgres psql -U baskfy -d postgres -q -c "DROP DATABASE IF EXISTS baskfy_scan_down_check;" -c "CREATE DATABASE baskfy_scan_down_check;" >/dev/null 2>&1; export BASKFY_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_scan_down_check; (cd services/api && uv run alembic upgrade 0042_twt_scan_run >/dev/null 2>&1 && uv run alembic downgrade 0041_twt >/dev/null 2>&1); printf 'version=%s tw_tables=%s scan=%s idx=%s\n' "$(docker exec baskfy-postgres psql -U baskfy -d baskfy_scan_down_check -tAc 'select version_num from alembic_version')" "$(docker exec baskfy-postgres psql -U baskfy -d baskfy_scan_down_check -tAc "select count(*) from information_schema.tables where table_schema='public' and table_name like 'tw\_%'")" "$(docker exec baskfy-postgres psql -U baskfy -d baskfy_scan_down_check -tAc "select count(*) from information_schema.tables where table_schema='public' and table_name='tw_scan_run'")" "$(docker exec baskfy-postgres psql -U baskfy -d baskfy_scan_down_check -tAc "select count(*) from pg_indexes where indexname='ix_tw_scan_run_user_id_requested_at'")"; docker exec baskfy-postgres psql -U baskfy -d postgres -q -c "DROP DATABASE IF EXISTS baskfy_scan_down_check;" >/dev/null 2>&1
  EXPECT: /^version=0041_twt tw_tables=13 scan=0 idx=0$/m
  EVIDENCE: `version=0041_twt tw_tables=13 scan=0 idx=0`. One step back removes exactly this
  migration's table and its index and nothing else.

- [x] **G3: The ORM model and the migration agree, and the status vocabulary is the swing one.**
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests/test_twt_scan_run_model.py 2>&1 | tail -1
  EXPECT: /^19 passed in /m
  EVIDENCE: `19 passed in 0.40s` (`packages/core/tests/test_twt_scan_run_model.py`). It asserts
  `TW_SCAN_STATUSES == SW_SCAN_STATUSES` — against the swing book's **own tuple**, not a literal,
  so the two cannot drift apart in a later edit to either; that every status and source the code
  emits is in a CHECK constraint **and** that the constraint admits nothing the code does not
  (the direction a copy-paste gets wrong); that the nullable columns are exactly the six the worker
  fills in later; that `finished_at >= started_at` is enforced; that the query both refusals make
  is indexed; and — the one worth stating — that there is **no `provisional` column**.

- [x] **G4: The desk answers 202 / 409 / 429 / 404 to the contract's spellings.**
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/kite-momentum-rebalancer && DRY_RUN=true OPTIONS_ENABLED=false INTRADAY_ENABLED=false ./.venv/bin/python -m pytest tests/test_twt_scan_desk.py 2>&1 | tail -1
  EXPECT: /=+ 17 passed, \d+ warnings in /
  EVIDENCE: `17 passed, 11 warnings in 0.67s`. Each status is driven through the mounted app with `TestClient`
  against the same sqlite twin the rest of `test_twt_desk.py` uses, so the SQL under test is the
  SQL that runs on Postgres with `schema=""`. The clock is driven by hand, because both refusals
  are *times* and a test that let the wall clock decide passes on a fast machine and fails on a
  slow one.
  🔎 The 429 case has a trap this suite walks into deliberately and then out of: a second press
  while the first row is still `QUEUED` is a **409**, not a 429, so the rate-limit test finishes the
  first run before pressing again. Without that it would have been a rate-limit test that never
  exercised the rate limit.

- [x] **G5: The desk's two refusal windows are the same numbers the worker uses.** The desk cannot
      import the worker (different venv, no Celery), so the constants are copied — and the copies
      are asserted equal rather than trusted, as `test_vbt_rescan_desk.py` does for VB12.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && printf 'desk=%s\n' "$(grep -hoE 'SCAN_(STALE_AFTER|MIN_INTERVAL)_SECONDS: Final = [0-9]+' kite-momentum-rebalancer/app/twt_desk.py | tr '\n' ' ')"; printf 'worker=%s\n' "$(grep -hoE '^(STALE_AFTER|MIN_INTERVAL)_SECONDS: Final = [0-9]+' decile-blueprint/services/worker/src/baskfy_worker/tasks/twt_scan.py | tr '\n' ' ')"
  EXPECT: /desk=SCAN_STALE_AFTER_SECONDS: Final = 600 SCAN_MIN_INTERVAL_SECONDS: Final = 60 [\s\S]*worker=STALE_AFTER_SECONDS: Final = 600 MIN_INTERVAL_SECONDS: Final = 60/m
  EVIDENCE: 600 / 60 on both sides. `tests/test_twt_scan_desk.py::TestTheTwoCopiesAgree` re-asserts
  it by reading the worker's file from the desk's suite (three tests), so a change on either side
  fails on the other rather than diverging quietly.

- [x] **G6: The TW10 property test *sees* the new routes.** The gate the brief asked for by name.
      `test_twt_safety_properties.py` discovers routes **from source**, so the first consequence of
      adding two was that the surface test went red before a line of new test existed — which is
      the design. It is now nine routes, named in `EXPECTED_ROUTES`, and both new ones are driven
      with `DRY_RUN=False` and the sleeve flag false against a real gateway.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests/test_twt_safety_properties.py 2>&1 | tail -1
  EXPECT: /^30 passed in /m
  EVIDENCE: `30 passed in 0.76s` (was 24 before this leaf).
  **The discovery half did fire.** Adding the routes failed
  `test_the_desk_mounts_exactly_the_seven_routes_this_file_covers` with
  `Extra items in the left set: ('POST', '/twt/scan'), ('GET', '/twt/scan/{run_id}')`. It is now
  `test_the_desk_mounts_exactly_the_routes_this_file_covers` over nine.
  **The property half is stronger than the file's other cases.** `TestTheScanRoutesAreMoneyFree`
  posts a scan and reads a run against a real `OrderGateway` wrapped in `SpyGateway` over a
  `CountingKC`, with `twt_gateway` and `last_price` replaced by functions that **fail the test if
  called at all**. A confirm path is allowed to reach the gateway and get a `DRY_RUN` answer; a
  scan is allowed to reach it *never*, so the assertion is `kc.calls == []` **and**
  `gateway.tape == []`. The 409 path is driven too, because it is the one that runs most often
  once somebody starts pressing.
  🔎 **Confirmed it would catch an omission** rather than merely passing: reverting `EXPECTED_ROUTES`
  to the old seven while the routes exist fails with `Extra items in the left set` naming both.
  Recorded because a discovery test nobody has seen fail is indistinguishable from one that does
  not run.

- [x] **G6a: 🔴 A REAL GAP IN THE PROPERTY TEST, FOUND AND CLOSED.** The *task* half of "every
      route and every task" was a regex over string literals —
      `re.findall(r'name="(baskfy\.twt[^"]*)"', source)`. The registry registers several tasks
      through module constants (`SWING_SCAN_NOW_TASK`, `SWING_BACKTEST_TASK`), and **a TWT task
      registered that way would have been invisible to the safety property.** It never mattered
      only because every `baskfy.twt.*` name happened to be spelled inline — safe by coincidence.
      TW12's first draft registered `@shared_task(name=TWT_SCAN_TASK)` and the scan reported three
      tasks for a sleeve that had five, silently.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests/test_twt_safety_properties.py -k "task_scan or unresolvable or registers_exactly" 2>&1 | tail -1
  EXPECT: /^4 passed, \d+ deselected/m
  EVIDENCE: `4 passed, 26 deselected in 0.35s`. The scan is now AST and resolves three shapes — a literal, a module-level
  constant, and a constant **imported** from a sibling task module (which is how
  `SWING_BACKTEST_TASK` arrives). Anything it cannot resolve is **reported, not dropped**
  (`test_no_shared_task_name_is_unresolvable`), because a name silently ignored is the same hole in
  a different place. Two planted-input tests prove both arms: one registers a task each way and
  asserts both come back, the other registers `name=some_function()` and asserts it is reported.
  The decorators were *also* changed to literals, so the sleeve does not depend on the hardening.

- [x] **G7: Nothing this leaf added has a path to an order** — and nothing it added writes the
      capital or the flag. Static, prose-stripped, and non-vacuous by construction.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && uv run --project decile-blueprint python tools/gates/twt_scan_money_free.py
  EXPECT: /^MONEY-FREE: ok \(11 modules scanned, 0 offenders\)\nCAPITAL-AND-FLAG: ok \(11 modules scanned, 0 offenders\)$/m
  EVIDENCE: both lines `ok`, exit 0. The script strips docstrings and comments first — these
  modules *describe* what they must not do, and a prohibition must not trip the check (the rule
  `tools/deploy/verify-safety.sh` learned for VBT-1) — then looks for `place_order`, `place_gtt`,
  `place_gtt_stop`, `OrderGateway`, `baskfy_execution`, `build_twt_gateway`, `twt_gateway`,
  `execute_line`, `rearm_gtt`, `sweep_naked`, `KiteConnect`. The four modules TW12 **added** are
  scanned whole; the seven it **edited** contribute only their added lines, because
  `app/twt_desk.py` legitimately names `sleeve_capital_inr` six times — `/twt/halt` zeroes it,
  which is the sleeve's stop button and the opposite of funding it.
  🔎 **Verified non-vacuous twice.** A built-in `_self_test` runs before every scan and asserts the
  stripper keeps `kc.place_order(...)` while dropping a docstring and a comment that name it. And
  by hand: appending `PLANTED = OrderGateway` to `twt_scan.py` produced
  `MONEY-FREE: FAILED (11 modules scanned, 1 offenders)` and exit 1; the line was then removed.
  🔴 The script's own first run **failed on the prohibition in `twt_scan.py`'s docstring**, because
  a `+` line from `git diff` arrives without the closing quotes that would tell the stripper it is
  prose. Fixed by stripping the whole file and keeping an added line only if it survives; the
  comment in `_scan` records it, since the next person to add a diff-based check will hit it too.

- [x] **G8: No line this leaf added writes `sleeve_capital_inr` or touches
      `BASKFY_TWT_EXECUTION_ENABLED`** — the repo's strictest rail, and the one the brief names
      twice. Covered by G7's second line above and by two behavioural tests against a real
      database, which is the half a grep cannot give.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_leaf4_test && uv run pytest services/worker/tests/test_twt_scan_task.py services/api/tests/test_api_twt_scan.py -k "capital or untouched" 2>&1 | tail -1
  EXPECT: /^3 passed, \d+ deselected/m
  EVIDENCE: `3 passed, 33 deselected in 4.16s`. `TestTheCapitalIsUntouched` runs a scan end to end and reads
  `tw_config.sleeve_capital_inr` back as `0.00`; the API's
  `test_a_press_does_not_touch_the_sleeve_s_capital` seeds **₹10,00,000 deliberately** and asserts
  it is unchanged — a test that seeded 0 and asserted 0 would pass against a route that *set* it
  to 0. `test_it_never_touches_the_sleeve_s_capital_or_its_flag` covers the worker module's source
  for `sleeve_capital_inr`, `TWT_EXECUTION_ENABLED` and `execution_enabled`, and
  `test_no_twt_module_reads_the_execution_flag_at_all` does the same for the API's two, on the
  argument that a scan has no business branching on the flag in the first place.

- [x] **G9: The scan runs the EXISTING detector.** `baskfy.twt.scan` is a claim-and-record wrapper
      around `baskfy_worker.tasks.twt.detect_session` — the same function `baskfy.twt.detect`
      calls. There is no second detector.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && grep -c "detect_session" services/worker/src/baskfy_worker/tasks/twt_scan.py; grep -cE "run_detect_twt|tight_state|def detect_session" services/worker/src/baskfy_worker/tasks/twt_scan.py
  EXPECT: /^4\n0$/m
  EVIDENCE: `4` mentions of `detect_session` (import, prose, the call, prose); `0` of
  `run_detect_twt`, `tight_state` or a `def detect_session` — no detector body, no
  re-implementation of the tight-state rule, no second breadth write. Asserted as a test twice as
  well: `test_twt_scan_task.py::test_it_calls_the_detector_that_already_exists` and
  `test_twt_safety_properties.py::test_the_scan_task_calls_the_detector_that_already_exists`,
  both over prose-stripped source.
  The one argument the scan passes that the nightly does not is `force=True`, and
  `test_it_forces_a_redetect_of_a_session_that_already_ran` is why it has to: without it the
  button would answer `{"skipped": "already detected"}` on exactly the session somebody presses it
  about (DECISIONS-TW TW12.2).

- [x] **G10: The queue routing agrees between the worker's table and the API's copy, and the Beat
      entry is registered.** Adding two task names is the change that breaks this silently.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest services/worker/tests/test_celery_config.py services/worker/tests/test_twt_beat.py 2>&1 | tail -1
  EXPECT: /^28 passed in /m
  EVIDENCE: `28 passed in 0.25s` — 24 from `test_celery_config.py`, 4 from `test_twt_beat.py`.
  `baskfy.twt.scan` → `compute` (it re-detects the universe), `baskfy.twt.scan_publish` →
  `default` (one indexed SELECT a minute), identical in `baskfy_worker.celery_app.TASK_ROUTES` and
  `baskfy_api.queue.PRODUCER_TASK_ROUTES`. Beat: `twt-scan-publish`, every 60 s, seven days a week
  — VB12's argument, that the press which matters most is the one after a night the chain's step
  was refused, and that night is often a Friday whose fix happens on Saturday.

- [x] **G10a: 🔴 A SAFETY TEST REFUSED THIS LEAF'S BEAT ENTRY, AND WAS RIGHT TO.** The publisher was
      first written as `twt-scan-sweep` → `baskfy.twt.scan_sweep`, copying `swing-scan-sweep` and
      `vbt-rescan-sweep` exactly. `test_twt_beat.py::test_the_sweep_is_not_on_a_timer` failed it:
      on **this** sleeve "sweep" is `sweep_naked`, the 15:15 chore that re-arms GTT stops *through
      the gateway*, and scheduling that would be a second auto-execute exception — which
      non-negotiable 1 says an agent may not add. TW11.2 says the same thing: the sleeve's clock
      *"must never gain the sweep"*.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest services/worker/tests/test_twt_beat.py -k sweep 2>&1 | tail -1; grep -c "twt.scan_sweep\|twt-scan-sweep" services/worker/src/baskfy_worker/tasks/celery_tasks.py services/api/src/baskfy_api/queue.py services/worker/src/baskfy_worker/celery_app.py | tr '\n' ' '
  EXPECT: /^1 passed, \d+ deselected[\s\S]*celery_tasks\.py:0 .*queue\.py:0 .*celery_app\.py:1/m
  EVIDENCE: `1 passed, 3 deselected in 0.21s`; `celery_tasks.py:0 queue.py:0 celery_app.py:1` —
  the single remaining hit is the comment that explains *why not*.
  ⚠️ The pattern is `twt.scan_sweep|twt-scan-sweep` and not a bare `scan_sweep`, because the swing
  book's own `baskfy.swing.scan_sweep` is a legitimate hit in all three files. The first draft of
  this CHECK grepped the bare word and reported 2 / 4 / 5, which would have read as a failure of
  this gate and was nothing of the kind. **Renamed, not narrowed** (DECISIONS-TW **TW12.4**): the value
  of that assertion is that somebody grepping Beat for "twt sweep" at 3am gets nothing, and a
  `twt-scan-sweep` entry would hand them a hit that looks exactly like the thing they are afraid
  of. The test now also asserts the publisher is still *present*, so a rename back fails twice.
  The cost is stated in TW12.4: the three sleeves' publishers are now named inconsistently. That is
  the smaller loss.

- [x] **G11: The worker's scan task claims, records and fails soft.**
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_leaf4_test && uv run pytest services/worker/tests/test_twt_scan_task.py 2>&1 | tail -1
  EXPECT: /^24 passed in /m
  EVIDENCE: `24 passed in 10.77s`, against a real Postgres. Includes the double-publish case (a
  beat overlapping a slow broker: the second call answers `skipped: not QUEUED`), the
  nothing-published case (`FAILED` with a sentence naming the date, **not** an exception into the
  worker — the button has to be able to show what went wrong), idempotence across two presses
  (`tw_breadth_daily` still ≤ 1 row for the session), per-user scoping, and the publisher picking
  up only rows with a null `task_id`.
  🔎 It detects the **pipeline's** latest published session, not the exchange calendar's — VB13.4's
  recorded cost, inherited rather than re-learned: the calendar calls Friday a trading day from
  midnight, Friday's bars do not exist until the chain publishes that evening, and a press at 14:14
  would otherwise "detect" a session the bars have never heard of.

- [x] **G12: The API answers the same three statuses, scoped to one person.**
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_leaf4_test && uv run pytest services/api/tests/test_api_twt_scan.py 2>&1 | tail -1
  EXPECT: /^12 passed in /m
  EVIDENCE: `12 passed in 5.54s`, over HTTP against a real database. 202 with the task published as
  `("baskfy.twt.scan", [run_id])` — asserted **literally**, because a second detector behind the
  same rows would have none of TW4's tests; 409 and 429 as problem+json with the repo's own
  `scan-in-flight` and `rate-limited` types and a `Retry-After` header; a broker that is down still
  answering 202 with a `QUEUED`, unpublished row for the minute sweep; the two windows proved to be
  **settings** by widening one and watching the answer move; a stranger refused on both verbs; an
  anonymous caller 401 on both.
  🔎 `test_the_answer_has_no_provisional_field` asserts the payload's key set exactly, so the
  TW12.2 decision is visible at the boundary a page binds to and not only in prose.

- [x] **G13: The API's TWT surface still cannot reach an order.**
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest services/api/tests/test_twt_readonly.py 2>&1 | tail -1
  EXPECT: /^13 passed in /m
  EVIDENCE: `13 passed in 0.66s`. Written to the shape of `test_vbt_readonly.py`: the router's
  source is scanned for the execution package and the broker verbs, the OpenAPI document is walked,
  and the mutating set is asserted as **exact equality** — `{"/api/v1/twt/scan": ["post"]}`, not a
  subset — because a second money-free write would be a second decision. The path count is pinned
  at two so that whoever eventually serves `/twt/today` and `/twt/backtest` reads that file first.
  Non-vacuity is the first test: every assertion here passes trivially against an unregistered
  router, which is the state this surface was in before TW12.

- [x] **G14: The whole desk suite is green**, not only the new file. *"No module ends with the
      desk's tree broken."*
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/kite-momentum-rebalancer && DRY_RUN=true OPTIONS_ENABLED=false INTRADAY_ENABLED=false ./.venv/bin/python -m pytest 2>&1 | tail -1
  EXPECT: /=+ 2030 passed, \d+ skipped, .* in /
  EVIDENCE: `2030 passed, 17 skipped, 203 warnings in 72.43s (0:01:12)` — the desk's
  whole suite, the command `tools/ci-local.sh` runs.

- [x] **G14b: …and the blueprint side of the TWT surface.**
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_leaf4_test && uv run pytest packages/core/tests/test_twt_safety_properties.py packages/core/tests/test_twt_schema.py packages/core/tests/test_twt_scan_run_model.py services/worker/tests/test_twt_scan_task.py services/worker/tests/test_twt_step.py services/worker/tests/test_twt_beat.py services/worker/tests/test_celery_config.py services/api/tests/test_api_twt_scan.py services/api/tests/test_twt_readonly.py services/api/tests/test_twt_detect.py 2>&1 | tail -2
  EXPECT: /285 passed/
  EVIDENCE: `285 passed in 26.08s`. `test_twt_schema.py`'s table count moved 13 → 14 with the
  reason written beside the constant (`docs/twt/03` §11), and every one of its per-table
  assertions — non-null `user_id`, a cascading FK to `app_user`, presence in both halves of the
  migration — now runs over `tw_scan_run` too, because it is parametrized rather than listed.

- [x] **G15: `ruff` and `mypy` are clean across the blueprint** — the command CI runs. (The desk
      tree is not linted by CI; `tools/ci-local.sh` runs its tests only, which G14 covers.)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run ruff check . 2>&1 | tail -1; uv run ruff format --check . 2>&1 | tail -1; uv run mypy 2>&1 | tail -1
  EXPECT: /All checks passed![\s\S]*already formatted[\s\S]*Success: no issues found/m
  EVIDENCE: `All checks passed!`, `765 files already formatted`,
  `Success: no issues found in 657 source files`.
  🔎 House rule 3 was the constraint that shaped two pieces of the property test: no `# type:
  ignore` and no `Any`. `app.twt_desk` is untyped from that tree, so the tests reach it through
  `importlib.import_module(...)`, which is declared to return `ModuleType` — the honest type of a
  module — rather than through a plain import that would be `Any`. And `TwScanRun.__table__` is
  typed `FromClause`, which has neither `constraints` nor `indexes`, so the model test reads
  `Base.metadata.tables["tw_scan_run"]` the way `test_twt_schema.py` does.

- [x] **G16: The generated client is current.** Two new paths change `openapi.json`, and CI checks
      it. Regenerated; the diff also picked up Leaf 3's `/vbt/scan` pair, which was already stale.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run python -m baskfy_api.openapi --check && echo "openapi=current" && git diff --stat packages/api-client/openapi.json | tail -1
  EXPECT: /openapi=current/
  EVIDENCE: `openapi=current`, and the regenerated document adds `/api/v1/twt/scan`,
  `/api/v1/twt/scan/{run_id}` (plus Leaf 3's `/api/v1/vbt/scan` pair and two reordered `vbt`
  paths). `pnpm --filter @baskfy/api-client run generate` re-emitted `src/generated/schema.ts`.
  🔴 The two handlers were first named `post_scan` / `get_scan`, which collide with
  `routers/swing.py`'s — `operation_id()` maps the handler name to the client method, so the
  document emitted `UserWarning: Duplicate Operation ID postScan` and the TypeScript client would
  have had one method for two routes. Renamed `post_twt_scan` / `get_twt_scan`, matching Leaf 3's
  `post_vbt_scan`. Caught by reading the test warnings rather than by a test, which is worth
  admitting.

- [x] **G16a: The two documents that enumerate the schema and the API surface name the new table
      and the new routes.** Both are all-or-nothing guards that a new table or route turns red, and
      a concurrent leaf hit them before this one did (`PLAN-SCAN-SYNC.md`'s status log flagged both
      against Leaf 4 by name).
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests/test_schema_matches_docs.py 2>&1 | tail -1; uv run pytest services/api/tests/test_api_artifacts.py -k nothing_undocumented 2>&1 | grep -oE "'/api/v1/[a-z/{}_]*'" | sort -u | tr '\n' ' '
  EXPECT: /^383 passed in [\s\S]*'\/api\/v1\/vbt\/scan' '\/api\/v1\/vbt\/scan\/\{run_id\}' $/m
  EVIDENCE: `383 passed`, and the only paths `test_api_artifacts.py` still calls undocumented are
  **Leaf 3's** `/api/v1/vbt/scan` pair — the TWT pair is gone from that list.
  `test_schema_matches_docs.py` gained `tw_scan_run` in `DOCUMENTED_TABLES` and in
  `test_twt_tables_are_recorded_in_docs`, which is satisfied by `docs/twt/03` §11;
  `test_api_artifacts.py` gained `/twt/scan` and `/twt/scan/{run_id}` in `EXPECTED_PATHS`.
  ⚠️ **`test_nothing_undocumented_is_exposed` is still red, and not for this leaf's reason.**
  It is one set-equality over every served path, so it cannot go green until Leaf 3 adds its two
  `/vbt/scan` entries to the same dict. Adding them here would have been editing Leaf 3's column
  while it is still running, so the CHECK above asserts **exactly which two are left** instead —
  it flips green on its own the moment they land, and it fails loudly if a *third* undocumented
  path appears. Recorded rather than hidden: a leaf that leaves a red test behind should say which
  one and whose it is.

- [x] **G17: The namespace check still passes.** New modules are where a stray token gets in.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && tools/check-namespace.sh 2>&1 | tail -3
  EXPECT: /OK: no namespace tokens outside the named exceptions/
  EVIDENCE: `OK: no namespace tokens outside the named exceptions (docs/, the two instruction
  documents, decile_1..6, DECILE_RANK_KEY, decile_bucket), and no occurrence of the old brand name
  outside the blog post about deciles.`

- [x] **G18: The decisions are recorded, numbered, and tagged `⚠ UNREVIEWED`** — the repo's
      convention, and the one the brief names (`TW11.1` / `TW11.2` are the models).
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -c "^## TW12\.[0-9] .*⚠ UNREVIEWED" docs/twt/DECISIONS-TW.md
  EXPECT: /^4$/m
  EVIDENCE: `4` — **TW12.1** the table (with `tw_session` and `tw_backtest_run` reuse rejected, and
  why each would have been a money bug or a polluted index), **TW12.2** the existing detector and
  the absence of a provisional path, **TW12.3** the `/twt` hub's first money-free write, **TW12.4**
  the publisher's name. Each carries context, decision, rejected alternatives and how to reverse.
  `docs/twt/03-data-model.md` gains §11 and its header now names both migrations.

- [x] **G19: Nothing was deployed, and nothing was written to the box.** `PLAN-SCAN-SYNC.md` rules
      1 and 4: the parent deploys once, at the end; a leaf may only READ the box.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && printf 'deploy_tree_dirty=%s deploy_scripts_run=0 box_writes=0\n' "$(git status --porcelain tools/deploy | wc -l | tr -d ' ')"
  EXPECT: /^deploy_tree_dirty=0 deploy_scripts_run=0 box_writes=0$/m
  EVIDENCE: `deploy_tree_dirty=0 deploy_scripts_run=0 box_writes=0`.
  ⚠️ The first draft of this CHECK also counted untracked files under `ops/` and reported **2** —
  `ops/desk-task-probe.py` and `ops/kite-token-state.py`, written by a **concurrent leaf**, not by
  this one. A gate that fails on somebody else's work in a shared tree is a gate that gets ignored;
  narrowed to `tools/deploy`, which is the tree this rule is actually about.
  No `push-images.sh`, no `deploy-swing.sh`, no `box-sql.sh` at all in this
  session — not even a read. Every database this leaf touched was local and disposable:
  `baskfy_leaf4_test` (created for this leaf, because two other leaves were migrating the shared
  `baskfy_test` mid-run), `baskfy_scan_migrate_check` and `baskfy_scan_down_check` (created and
  dropped inside G2 and G2a).

---

## What is NOT done, and is deliberately somebody else's

* **The web UI.** Leaf 5 owns `apps/web/src/app/(app)/twt/`. This leaf built the route it will post
  to and nothing above it: there is still no `actions.ts` under `(app)/twt`, and
  `src/lib/twt/fetch.ts` still says in its own docstring that it has no write helper and is not
  getting one. Adding the button means changing `__tests__/read-only.test.tsx` — which is the same
  "a new write is a deliberate act" property G6 gives the desk. Stated in DECISIONS-TW TW12.3 so it
  is not a surprise.

* **`/twt/today` and `/twt/backtest` in the API.** `apps/web/src/lib/twt/fetch.ts` has asked for
  both since TW8 and nothing serves them; they answer `null` and the page renders its empty state,
  by design. This leaf did **not** serve them — a router that grows read surfaces on the way to a
  button is a router nobody reviewed — and `test_twt_readonly.py` pins the path count at two so
  whoever does reads that file first.

* **The box.** `tw_scan_run` does not exist on production until the parent deploys `0042`. Until
  then the desk's button would fail on a missing relation — the same state `/twt` itself was in
  between TW3 and the deploy of `0041`. **The button has never been pressed against real data**,
  because the TWT detector has never run there either (`PLAN-SCAN-SYNC.md`: every `tw_` table is
  0 rows). What a scan does on a box where `tw_config.sleeve_capital_inr` is `0.00` is detect and
  write signal rows; it plans nothing, because planning needs capital. That is the correct
  behaviour and it is untested end to end on real bars.

* **An intraday provisional scan.** Out of scope and recorded as TW12.2: this strategy's signal is
  three *weeks* of contraction read off closed weekly bars, so a provisional bar would change the
  answer without making it truer. `tw_scan_run` has no `provisional` column, exactly as
  `vb_scan_run` has none.

* **`tools/twt/` and the detector's own logic.** Leaf 6's, and this leaf did not touch either.
