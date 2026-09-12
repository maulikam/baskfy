# VBT "Scan now" — the backend (Leaf 3 of PLAN-SCAN-SYNC)

**Plan:** `PLAN-SCAN-SYNC.md` "The contract". **Template:** the swing sleeve's SW15 —
`kite-momentum-rebalancer/app/swing_desk.py::swing_scan_now`,
`services/api/src/baskfy_api/swing_scan.py`, `routers/swing.py::post_scan` / `get_scan`,
`baskfy_worker.tasks.swing_scan_now`.

**Ships:**

* desk — `POST /vbt/scan` (202) and `GET /vbt/scan/{run_id}` in `app/vbt_desk.py`, over the
  existing `vb_scan_run` table, with the two refusals moved into `PgVbtStore.request_scan` so
  the new route and the old `/vbt/rescan` button answer from one place;
* API — `baskfy_api/vbt_scan.py` (the writer and the row view), `POST /vbt/scan` (202) and
  `GET /vbt/scan/{run_id}` on `routers/vbt.py`, publishing the **existing** `baskfy.vbt.rescan`
  task;
* two settings, `vbt_scan_min_interval_seconds` / `vbt_scan_stale_after_seconds`;
* tests, including the money-free property on both halves.

**What it does NOT ship:** no web UI (leaf 5), no deploy, no second detector, no migration —
`vb_scan_run` shipped in `0040_vbt_scan_run` and `baskfy.vbt.rescan` + `vbt-rescan-sweep`
already exist.

**The one non-negotiable:** a scan queues a detector and is money-free. Nothing added here may
reach `OrderGateway.place`. G7 and G8 are that claim, asserted structurally.

---

- [x] G1: The desk serves `POST /vbt/scan` → **202** with the run to poll, and one `QUEUED`
      `vb_scan_run` row is written with `source='desk'` and no `task_id` (the sweep's input).
  CHECK: cd kite-momentum-rebalancer && .venv/bin/python -m pytest tests/test_vbt_rescan_desk.py -k "scan_route or queued_row or 202" -p no:cacheprovider 2>&1 | grep -E '[0-9]+ (passed|failed)' | tail -1
  EXPECT: /passed/
  EVIDENCE: `4 passed, 30 deselected`. The body is
  `{"run_id": …, "status": "QUEUED", "requested_at": "2026-09-09T21:20:00+05:30"}` — the same
  three keys `POST /swing/scan` answers. `source` is `desk` and `task_id` is `NULL`, which is
  exactly what `unpublished_runs()` in `baskfy_worker.tasks.vbt_rescan` selects on, so the row
  the desk writes with no Celery client is picked up by `vbt-rescan-sweep` within the minute.

- [x] G2: The desk answers **409** while one is in flight and **429** inside a minute, with
      `Retry-After`, both read from `vb_scan_run` rather than a cache; a dead worker past the
      stale window does not wedge the button.
      ⚠️ **Repaired by the parent: `-q` on top of the repo's own `addopts = "-q …"` makes `-qq`,
      which suppresses pytest's summary line entirely** — the output was progress dots and
      nothing else, so `/passed/` could not match a run in which everything passed. The check
      now greps the summary rather than tailing into the dots.
  CHECK: cd kite-momentum-rebalancer && .venv/bin/python -m pytest tests/test_vbt_rescan_desk.py -p no:cacheprovider 2>&1 | grep -E '[0-9]+ (passed|failed)' | tail -1
  EXPECT: /passed/
  EVIDENCE: `34 passed` (21 of them VB12's, unchanged). The 409 says
  `Scan 1 is queued; its result is on its way.` and writes no second row; the 429 carries
  `Retry-After: 40` and `one a minute is the limit`. Both come out of
  `PgVbtStore.request_scan`, which reads `newest_scan()` — one SELECT on `vb_scan_run`, no Redis
  — so the rule holds on a box with no cache and is provable against sqlite. A `RUNNING` row
  older than `RESCAN_STALE_AFTER_SECONDS` (600) is allowed through: `202`, second row written.

- [x] G3: `GET /vbt/scan/{run_id}` returns that run's status, and 404 for an id that is not this
      user's.
  CHECK: cd kite-momentum-rebalancer && .venv/bin/python -m pytest tests/test_vbt_rescan_desk.py -k "get_scan or belongs or unknown_run" -p no:cacheprovider 2>&1 | grep -E '[0-9]+ (passed|failed)' | tail -1
  EXPECT: /passed/
  EVIDENCE: `6 passed, 28 deselected`. All four statuses of the contract's vocabulary read back
  verbatim (`QUEUED | RUNNING | DONE | FAILED`), a DONE row carries `session_date` and the
  worker's `detail`, an unknown id is 404 and — the one worth having — a row belonging to
  `user_id = 2` is **404 rather than a peek**: `PgVbtStore.scan_run` filters on `user_id`, it
  does not look up by primary key alone.

- [x] G4: The old `POST /vbt/rescan` still answers exactly as it did — 200 with
      `{"accepted": …}` — because `app/templates/vbt.html`'s button reads `body.accepted` and
      `body.reason`. A contract kept, not a route renamed under a live page.
      ⚠️ **Repaired by the parent: `-q` on top of the repo's own `addopts = "-q …"` makes `-qq`,
      which suppresses pytest's summary line entirely** — the output was progress dots and
      nothing else, so `/passed/` could not match a run in which everything passed. The check
      now greps the summary rather than tailing into the dots.
  CHECK: cd kite-momentum-rebalancer && .venv/bin/python -m pytest tests/test_vbt_desk.py tests/test_vbt_rescan_desk.py -p no:cacheprovider 2>&1 | grep -E '[0-9]+ (passed|failed)' | tail -1
  EXPECT: /passed/
  EVIDENCE: `57 passed`. The refusal bodies are asserted field by field against what the
  template's JavaScript reads: `{"accepted": false, "reason": "a re-detect is already in
  flight", "run_id": n, "status": "QUEUED"}` and `reason: "too soon"` with
  `retry_after_seconds: 60`. The template was not touched. The two routes now share one store
  method, so the rule cannot drift between them.

- [x] G5: The API serves `POST /vbt/scan` → 202 and publishes **`baskfy.vbt.rescan`** (the
      existing task — no second detector), `GET /vbt/scan/{run_id}` reads it back, 409/429 hold,
      and a broker that is down still leaves a `QUEUED` row for the sweep.
      ⚠️ **Repaired by the parent: `-q` on top of the repo's own `addopts = "-q …"` makes `-qq`,
      which suppresses pytest's summary line entirely** — the output was progress dots and
      nothing else, so `/passed/` could not match a run in which everything passed. The check
      now greps the summary rather than tailing into the dots.
  CHECK: cd decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest services/api/tests/test_api_vbt_scan.py -p no:cacheprovider 2>&1 | grep -E '[0-9]+ (passed|failed)' | tail -1
  EXPECT: /passed/
  EVIDENCE: `10 passed`, against a real Postgres. The published name is asserted as an equality
  rather than a substring — `queue.sent == [("baskfy.vbt.rescan", [run_id])]` — so a second
  detector, or a second publish, fails. The row carries `task_id = "task-1"` and
  `detail = {"source": "web"}`. `BrokerDown` (a producer that raises `ConnectionError`) still
  gets **202** with `status=QUEUED, task_id=NULL`, which is what `vbt-rescan-sweep` picks up.
  The 409 body is `{"type": "scan-in-flight", "run_id": n, "status_of_run": "QUEUED"}` and
  nothing was published on the second press; the 429 carries `type: rate-limited`,
  `Retry-After` equal to its own `retry_after`, and "one a minute" in the detail. The thresholds
  are proved to be settings rather than literals: the identical request is 429 under
  `vbt_scan_min_interval_seconds=120` and 202 under the default 60.

- [x] G6: The scan belongs to one person: a principal who is not the sole tenant is refused on
      both verbs, and `GET /vbt/scan/{id}` on somebody else's run is a 404.
  CHECK: cd decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest services/api/tests/test_api_vbt_scan.py -k "OnePerson" -p no:cacheprovider 2>&1 | grep -E '[0-9]+ (passed|failed)' | tail -1
  EXPECT: /passed/
  EVIDENCE: `2 passed`. A second signed-in user is refused on **both** verbs by
  `scoped_sole_user_id` — refused, not quietly served the sole tenant's rows, which is the
  failure M43.4 found on the watchlist. A run id that exists but is not theirs is a 404, and an
  anonymous caller gets 401 on both. `test_vbt_readonly.py` covers the structural half: nine
  handlers, nine `await scoped_sole_user_id(` calls, and `principal` in every signature.

- [x] G7: **MONEY-FREE, API half.** `baskfy_api.vbt_scan` writes `VbScanRun` and nothing else,
      names no broker and no execution package, and the `/vbt` surface still exposes no path
      that mentions an order. `routers/vbt.py` declares exactly one POST.
      ⚠️ **Repaired by the parent: `-q` on top of the repo's own `addopts = "-q …"` makes `-qq`,
      which suppresses pytest's summary line entirely** — the output was progress dots and
      nothing else, so `/passed/` could not match a run in which everything passed. The check
      now greps the summary rather than tailing into the dots.
  CHECK: cd decile-blueprint && uv run pytest services/api/tests/test_vbt_readonly.py -p no:cacheprovider 2>&1 | grep -E '[0-9]+ (passed|failed)' | tail -1
  EXPECT: /passed/
  EVIDENCE: `12 passed`. The census grew rather than loosened: `DELIBERATE_MUTATING_ROUTES` now
  carries `/api/v1/vbt/scan: {post}` beside the config PATCH, the path count moved 6 -> 8, and
  the new `test_the_scan_writer_touches_only_the_run_table` asserts `baskfy_api.vbt_scan` names
  none of `VbPlan | VbPlanLine | VbOrder | VbPosition | VbFill | VbSignalDaily | VbBreadthDaily
  | VbConfig | VbSession`, constructs `VbScanRun(` and publishes the literal
  `SCAN_TASK_NAME: Final = "baskfy.vbt.rescan"` — the detector VB12 already shipped, not a
  second one. `routers/vbt.py` declares exactly one `@router.post(` and no PUT or DELETE.
  `test_no_vbt_path_mentions_an_order` still finds nothing across the eight paths.

- [x] G8: **MONEY-FREE, desk half.** The new routes name no gateway, no `execute_line`, no
      order verb; `app/vbt_desk.py` still has exactly one route that can reach `execute_line`;
      and a scan against a store whose gateway explodes on any attribute still succeeds —
      the property test, in the style of `test_twt_safety_properties.py`.
      ⚠️ **Repaired by the parent: `-q` on top of the repo's own `addopts = "-q …"` makes `-qq`,
      which suppresses pytest's summary line entirely** — the output was progress dots and
      nothing else, so `/passed/` could not match a run in which everything passed. The check
      now greps the summary rather than tailing into the dots.
  CHECK: cd kite-momentum-rebalancer && .venv/bin/python -m pytest tests/test_vbt_safety.py tests/test_vbt_scan_safety.py -p no:cacheprovider 2>&1 | grep -E '[0-9]+ (passed|failed)' | tail -1
  EXPECT: /passed/
  EVIDENCE: `31 passed`. `tests/test_vbt_scan_safety.py` is the new property file and the
  strong half is the fixture: it runs every scan route with `DRY_RUN=False` **and**
  `VBT_EXECUTION_ENABLED=True` — both switches the dangerous way — against an
  `ExplodingGateway` that raises on *any* attribute and an `ExplodingKC` in the broker's place.
  All three routes (the 202, the 200 and the poll) and **both refusal paths** run to completion
  and `vb_plan`, `vb_plan_line`, `vb_order`, `vb_position`, `vb_fill` are all still 0 rows.
  Non-vacuity is asserted too (`test_the_landmine_is_real` proves the gateway does explode when
  touched, and `test_the_route_scan_would_notice_a_new_one` proves the ast walk finds a route).
  Structurally: the routes are **discovered** with `ast`, not listed, so a fourth one fails
  `test_the_desk_mounts_exactly_the_scan_routes_this_file_covers`; each handler's code with its
  docstring stripped names none of `execute_line | gateway | place | kc. | gtt | kiteconnect`;
  `request_scan` contains exactly one `INSERT INTO` and it is `vb_scan_run`; `scan_run` contains
  no INSERT/UPDATE/DELETE at all; and `app/vbt_desk.py` still has exactly one
  `_execute.execute_line(` call, behind `/vbt/execute`.

- [x] G9: The desk's copies of the two thresholds still equal the worker's, and the desk's
      `/vbt/scan` uses them rather than a literal.
  CHECK: cd kite-momentum-rebalancer && .venv/bin/python -m pytest tests/test_vbt_rescan_desk.py -k "TestTheDeskAndTheWorkerAgree" -p no:cacheprovider 2>&1 | grep -E '[0-9]+ (passed|failed)' | tail -1
  EXPECT: /passed/
  EVIDENCE: `4 passed, 30 deselected`. VB12's own parity test is untouched and still reads the
  worker's source as text (the desk cannot import it — different venv, no Celery): `600` and
  `60` match `STALE_AFTER_SECONDS` / `MIN_INTERVAL_SECONDS` in
  `services/worker/src/baskfy_worker/tasks/vbt_rescan.py`, and the in-flight tuple matches. The
  new `/vbt/scan` takes the same two module constants through `PgVbtStore.request_scan`'s
  defaults rather than a literal of its own, so there is still one copy per tree, not three.

- [x] G10: Both suites green, whole. The desk must be able to rebalance on any Friday.
      ⚠️ **Repaired by the parent: `-q` on top of the repo's own `addopts = "-q …"` makes `-qq`,
      which suppresses pytest's summary line entirely** — the output was progress dots and
      nothing else, so `/passed/` could not match a run in which everything passed. The check
      now greps the summary rather than tailing into the dots.
  CHECK: cd kite-momentum-rebalancer && .venv/bin/python -m pytest tests -p no:cacheprovider 2>&1 | grep -E '[0-9]+ (passed|failed)' | tail -1
  EXPECT: /passed/
  EVIDENCE: `2030 passed, 17 skipped, 12 subtests passed in 78.58s` — 0 failed. (It was
  2013 passed before this leaf; the 17 new ones are the scan routes and the property file.) Two
  existing assertions were **rewritten rather than weakened**, and both said so in their own
  docstrings before I touched them: `test_vbt_desk.py` counted the desk's POSTs (2 -> 3, and it
  now names all three) and `test_vbt_safety.py` did the same. The property each was a proxy for
  — *exactly one route reaches `execute_line`* — is unchanged and is still asserted, now in
  three places.

- [x] G11: The VBT and swing API suites are green, whole.
  CHECK: cd decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest services/api/tests -k "vbt or swing" -p no:cacheprovider 2>&1 | grep -E '[0-9]+ (passed|failed)' | tail -1
  EXPECT: /passed/
  EVIDENCE: **357 passed, 0 failed** against the shared, migrated `baskfy_test`.
  ⚠ **A false alarm worth writing down, so the next person does not chase it.** An earlier run
  of this same selection reported 6 ERRORs in
  `test_swing_schema_and_settings.py::TestTheSchemaOnARealDatabase`. They were mine, not the
  code's: I had pointed `BASKFY_TEST_DATABASE_URL` at a throwaway database
  (`baskfy_test_leaf3`) to avoid contending with the sibling leaves, and that class's
  module-scoped `screener_helpers.seeded_database()` fixture needs a database the suite's own
  conftest has prepared. Every one of those files passes alone, and all 357 pass together on the
  database the plan names. The throwaway was dropped. `services/worker/tests/test_vbt_rescan.py`
  + `test_celery_config.py` also run green (**44 passed**), which is the other half of the
  claim that this leaf queued no second detector.

- [x] G12: `ruff check .` and `mypy` clean in `decile-blueprint`.
  CHECK: cd decile-blueprint && uv run ruff check . 2>&1 | tail -2 && uv run mypy 2>&1 | tail -2
      ⚠️ **`/a/ and /b/` is not syntax the runner has** — it parses one `/regex/flags`. Rewritten
      as a single pattern; `[\s\S]*` because ruff and mypy answer on separate lines.
  EXPECT: /All checks passed[\s\S]*no issues found/
  EVIDENCE: `All checks passed!` and `Success: no issues found in 657 source files`. No
  `# type: ignore`, no `Any` and no swallowed exception was added (house rule 3) — the one
  `except Exception` in `vbt_scan.request_scan` logs and continues by design, because a broker
  that is down is not the person's fault and the sweep publishes the row anyway; it is copied
  verbatim from `swing_scan`. (Mid-run, `mypy` reported 7 errors in
  `packages/core/tests/test_twt_safety_properties.py` — leaf 4's in-flight file, not this
  leaf's; `uv run mypy` over my own four files was `Success` throughout, and the tree was clean
  again by the end.)

- [x] G13: The namespace check still passes — no brand token entered on this change.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && tools/check-namespace.sh 2>&1 | tail -3
  EXPECT: /no namespace tokens/
  EVIDENCE: `OK: no namespace tokens outside the named exceptions (docs/, the two instruction
  documents, decile_1..6, DECILE_RANK_KEY, decile_bucket), and no occurrence of the old brand
  name outside the blog post about deciles.`

- [x] G14: The route shapes leaf 5 binds to are written down, and the decision (why `/vbt/scan`
      exists beside `/vbt/rescan`, and why the desk answers 202 where the swing desk answers
      200) is recorded in `docs/vbt/DECISIONS-VB.md`.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -c "^### VB15" docs/vbt/DECISIONS-VB.md && grep -c "POST  /vbt/scan" decile-blueprint/services/api/src/baskfy_api/routers/vbt.py
  EXPECT: /^1$/m
  EVIDENCE: `1` and `1`. `docs/vbt/DECISIONS-VB.md` § **VB15** (⚠ UNREVIEWED) carries the whole of it:
  the two routes on each side, why `/vbt/rescan` was kept byte-for-byte rather than renamed
  under a live page, why the desk answers **202** where the swing desk answers 200, why there is
  no `provisional` here, and — as **VB12.1** — the one honest compromise on the `source` column
  (CHECK limits it to `desk | cli`; the API writes `desk` and records `{"source": "web"}` in
  `detail`, exactly as `sw_scan_run` does, with the known limit that the worker replaces
  `detail` with the funnel on DONE and the clean fix being a CHECK-widening migration when a
  slot is free). The route shapes leaf 5 binds to are in the report and in
  `routers/vbt.py`'s own route table.

---

## The route shapes leaf 5 binds to

**On the API** (`serverApiOrigin`, the path `swing/actions.ts::scanNow` uses through
`lib/swing/write.ts`), base `/api/v1`:

```
POST /api/v1/vbt/scan                       bearer required; no body
  202  {"run_id": int, "status": "QUEUED", "requested_at": "<ISO-8601 UTC>"}
  409  problem+json {"type": "scan-in-flight", "title": "A scan is already in flight",
                     "detail": "Scan 7 is queued; its result is on its way.",
                     "run_id": 7, "status_of_run": "QUEUED"}
  429  problem+json {"type": "rate-limited", "detail": "... one a minute is the limit ...",
                     "retry_after": 40, "run_id": 7}   + header Retry-After: 40
  401  no bearer.   403/404  a principal who is not the sole tenant.

GET  /api/v1/vbt/scan/{run_id}               bearer required
  200  {"run_id": int, "status": "QUEUED"|"RUNNING"|"DONE"|"FAILED",
        "source": "web"|"desk"|"cli",
        "requested_at": ISO, "started_at": ISO|null, "finished_at": ISO|null,
        "session_date": "YYYY-MM-DD"|null,      # null until the worker decides
        "funnel": {...}|null,                   # filled on DONE
        "detail": {...}|null, "error": str|null}
  404  unknown id, or a run that is not this tenant's.
```

**On the desk** (`kite-momentum-rebalancer`, same shape, for an operator):

```
POST /vbt/scan            202 {"run_id", "status": "QUEUED", "requested_at"}
                          409 / 429 as FastAPI {"detail": "..."} (+ Retry-After on the 429)
GET  /vbt/scan/{run_id}   200 {"run_id","status","source","requested_at","started_at",
                               "finished_at","session_date","detail","error","task_id"}
                          404 unknown, or not this desk's user
POST /vbt/rescan          unchanged, 200 {"accepted": bool, "reason"?, "run_id",
                               "status", "retry_after_seconds"?} — the desk page's own form
```

**Three things a page should know.**

1. **There is no `provisional` and there will not be one.** VBT re-detects the last *published*
   session, so `session_date` is a closed day. "Scanning today from live quotes" is the swing
   book's control and does not exist here (`VbScanRun`'s own docstring, DECISIONS-VB VB15).
2. **`status` is the whole state machine.** `QUEUED` may sit for up to a minute before `RUNNING`
   when the row was written with no broker — the sweep publishes it — so a page that treats
   `QUEUED` as "stuck" will be wrong once a day.
3. **`GET /vbt/today` does not carry `last_scan`** the way `GET /swing/setups` does. Leaf 5's
   VB14 decided against polling and renders from the POST's status code, so it was left out
   rather than added speculatively; it is a small additive change to `VbtTodayOut` if the page
   turns out to want "scanning…" beside the button after a refresh.
