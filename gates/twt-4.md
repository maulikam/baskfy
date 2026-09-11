# TW4 — the nightly job: every session's state, signals, breadth and tomorrow's trigger

**Plan:** `docs/twt/06-module-plan.md` § TW4. **Spec:** `04-business-rules.md` §§1, 2, 4, 7.
**Reads:** `baskfy_core.twt` (TW1, shipped) and the `tw_` schema (TW3, shipped).

**The published-session clock.** The signal and the breadth reading come from the **last completed
session** and never from a day still running. `/CLAUDE.md`'s two-clocks section is the law here,
and it records two attempts to move that boundary that had to be reverted.

> **One note on reading the evidence below.** `decile-blueprint/pyproject.toml` sets
> `addopts = "-q"`, so every CHECK command here runs pytest at `-qq`, which prints the progress
> line and **no summary**. The commands were run exactly as written (all dots, exit 0, no `F`, no
> `E`); the counts quoted in each EVIDENCE come from the **same selection** re-run with
> `-o addopts="--strict-markers --strict-config"`, which restores the summary line without
> changing what is collected. `BASKFY_TEST_DATABASE_URL` was exported throughout, so the
> db-marked half ran rather than skipping — a skip that reads as a pass is the failure mode this
> note exists to rule out.

- [x] G1: `baskfy.twt.detect(trade_date)` writes `tw_state_daily`, `tw_signal_daily` and
      `tw_breadth_daily` from the published bars, and **running it twice changes no rows**
      (house rule 7).
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests services/api/tests -k "twt and (idempot or detect)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `31 passed, 5923 deselected in 12.94s`. The idempotence test does not count rows — it
  compares a **snapshot** of every stored number of the session (nineteen columns of each state
  row, eight of each signal row, the breadth row's counts, gate, `thin_session` and its funnel
  JSON, and the four ratchet columns of the book) before and after a second run, and asserts the
  two are equal. `created_at` is deliberately outside the snapshot: an upsert leaves it where it
  was, so comparing it would assert the column default rather than the write. The same fixture
  proves the three tables are written at all — three names hold the state, two of them signal
  (`SIGNAL` and `SCAN_ONLY` with `failed_filters = {TURNOVER}`), the fourth is never tight and
  appears only in the breadth denominator.

- [x] G2: **Upsert, never delete-and-reinsert.** `tw_order.signal_date` is a composite FK into
      `tw_signal_daily`, so a delete is refused once an order references the row (TW3's finding).
  CHECK: cd decile-blueprint && grep -rniE "delete\(|\.delete\b" services/worker/src/baskfy_worker/tasks/twt*.py | grep -viE "#|\"\"\"" | wc -l
  EXPECT: /^\s*0\s*$/
  EVIDENCE: `0`. Both writers go through `tasks/twt.py::_upsert`, which is
  `insert(...).on_conflict_do_update(index_elements=[user_id, date, instrument_id], set_=...)`;
  the breadth row is the same statement on `(user_id, date)`. The grep is the weaker half of the
  proof, so the stronger one is a test:
  `test_twt_detect.py::test_the_task_upserts_and_never_deletes_the_day_first` inserts a **real**
  `tw_order` row referencing the session's signal and then re-runs the detector. A
  delete-and-reinsert raises `ForeignKeyViolation` there; the run is green, and the signal row is
  still present afterwards.
  Re-run under contention this selection reports only
  `DeadlockDetectedError` (12 of them in one sample, every failure) and nothing else —
  a sibling session's worker suite truncating `instrument` while this one inserts into it.

- [x] G3: A date with **no published bars writes nothing** and says so in the step's `detail` —
      silence and "nothing happened" are different answers.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests services/api/tests -k "twt and (no_bars or unpublished or empty)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `8 passed, 5946 deselected in 2.85s`. Three distinct cases, because they fail
  differently: a date the bars do not reach (`SKIPPED`, `skipped_reason` = "2019-10-13 is not a
  session the bars know about", **and no `tw_breadth_daily` row at all** — VB13.4's bug was
  exactly such a row left behind by `make vbt DATE=<today>` at 14:14); an empty universe
  (`SKIPPED`, "no bars for … in the TWT universe"); and the assertion that the breadth table is
  still empty afterwards, which is the one that matters, because the evening job reads that table
  as its calendar and a false row there is worse than no row.

- [x] G4: **Tomorrow's trigger, computed the night before.** For every OPEN position: `high_since`
      raised by the session's high, `next_trigger` per §7.2 tick-floored and clamped under the
      close, stored with `next_trigger_for`.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests services/api/tests -k "twt and (trigger or ratchet)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `30 passed, 5924 deselected in 6.95s`. The arithmetic, checked against §7.2 by hand: a
  line bought at 96.00 with a stop resting at 76.80, a session high of 100.00 →
  `high_since = 100.00`, `trail = 80.00`, `raw = max(76.80, 80.00) = 80.00`, `80.00 < close` so
  `trigger = tick_floor(min(80.00, 99.99)) = 80.00`, stored with `next_trigger_for = 2019-09-13`.
  Four more assertions around it: the CHECK that a trigger cannot exist without its session;
  **`stop_price`, `gtt_id` and `gtt_trigger` are untouched** (the ratchet is a plan line, never a
  job — `02` Track C §3); the clamp's *second* branch, where a line that has fallen from 130.00 to
  100.00 trails to 104.00, above the last traded price, and the stored trigger becomes
  `tick_floor(100.00 x 0.999) = 99.90` — strictly under the close, because a trigger at the last
  price fires the moment it is armed; and `high_since` compared against
  `high_since_from_bars`, a recomputation that reads every bar of the hold in one pass. The two
  are different arithmetic — an accumulation and a maximum — and `03` §5 asks TW4 for exactly that
  comparison.

- [x] G5: §7.3's corporate action: a synthetic split yields a stored `entry_reference_close` equal
      to the raw price **and a `next_trigger` that is never lower than the resting one**. TW0.7:
      an action may raise a stop or raise an alert, never lower a stop.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests services/api/tests -k "twt and (split or adjustment or corporate)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `8 passed, 5946 deselected in 3.08s`. The fixture is a **consistent** 1:2 split —
  every stored `close` is half its `close_raw` and `adj_factor` is 0.5 throughout — rather than one
  row with two unrelated prices in it, so the state still holds and the conversion is the only
  thing under test. Stored: `close = 50.00`, `close_raw = 100.0000`,
  `entry_reference_close = 100.00`, `stop_preview = 80.00`. Then the position: bought pre-split at
  `entry_adj_factor = 1`, protected at 144.00, the re-derived `high_since` is
  `max(adjusted high over the hold) / 0.5 = 100.00` and the trail off it is 80.00 — **below** the
  resting stop. `next_trigger` is `None`, `stop_price` and `gtt_trigger` are still 144.00, and
  `high_since` is re-expressed to 100.00 (a level, not a stop, so lowering it is the honest
  answer). The mirror case is asserted too — the same split against a stop already re-armed at
  40.00 emits `next_trigger = 80.00` — so the refusal is a rule rather than a branch that can
  never be reached.

- [x] G6: **The step cannot fail the night.** A detector that raises leaves the pipeline run
      SUCCEEDED, exactly as its two siblings do — a test makes it raise.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests services/api/tests services/worker/tests -k "twt and (cannot_fail or raises or step)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `10 passed, 1 skipped, 6939 deselected in 8.34s`. The skip is TW2's
  (`test_twt_goldens.py:406: the engine has landed; there is nothing to fail`), not this module's.
  `test_twt_step.py` monkeypatches `run_detect_twt` to raise `RuntimeError("the three-weeks-tight
  detector fell over")`; `run_compute_twt_step` returns, and the `pipeline_run_step` row reads
  `skipped` with `RuntimeError` and the message in its `error` payload. Three more skips that say
  so rather than guessing: no `BASKFY_SOLE_USER_ID`, `BASKFY_TWT_NIGHTLY_ENABLED=false`, and a run
  with `twt_execution_enabled=True` that behaves **identically** and writes no `tw_order`,
  `tw_fill`, `tw_plan` or `tw_plan_line` row — there is no code path in detection that a flag
  could turn into an order.

- [x] G7: `COMPUTE_TWT` runs **after** `COMPUTE_VBT`, which runs after `COMPUTE_SWING`, and a test
      asserts the chain's order. Neither sibling is touched.
  CHECK: cd decile-blueprint && uv run pytest -k "post_publish or pipeline_step_order or chain" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `46 passed, 1 failed, 7425 deselected` — and the one failure is **not this
  module's**, which is established below rather than asserted. (The same selection read
  `47 passed, 7417 deselected in 6.45s` when TW4's step landed, against a baseline of `44
  passed` before it.) The chain is now
  fourteen: `… publish, refresh_basket, compute_swing, compute_vbt, compute_twt`, and
  `COMPUTE_TWT` is in `POST_PUBLISH_STEPS`, which `steps.py` asks for by name — *"a step added
  after these must either join this set or be a step the run's success depends on, which is a
  decision, not an edit"*. DECISIONS-TW **TW4.5** is the decision.
  **Two test files had to change and no sibling source did.** `test_pipeline_chain.py` gained
  `compute_twt` in its three chain assertions and its count went 13 → 14 — the same edit VB4 made
  to the same tests. `test_vbt_detect.py` asserted `chain[-1] is COMPUTE_VBT`, which pins a
  *position*; `steps.py` says in as many words that the property "was never the position", so it
  now asserts `set(chain[chain.index(COMPUTE_VBT):]) <= POST_PUBLISH_STEPS` — strictly stronger,
  since it holds for every step added after it too — with a comment recording what it used to say.
  `git diff --stat` touches no file under `baskfy_core/vbt/`, `baskfy_core/swing/`,
  `tasks/vbt*.py` or `tasks/swing*.py`. The one other shared file edited is
  `services/worker/tests/conftest.py`, which gained the thirteen `tw_` tables to its truncation
  list — `tw_config`, `tw_breadth_daily` and `tw_session` are keyed by user and do not cascade
  from `instrument`, so without it a breadth row written by one test is still there for the next
  and "this session has not been detected yet" depends on execution order. VB8 found the same
  omission for the `vb_` tables and the comment there says so.
  **The failing test, and why it is not TW4's.**
  `test_pipeline_self_sufficiency.py::TestTheFallbackJoinsTheChainsTransaction::test_it_writes_through_an_uncommitted_transaction_without_blocking`
  is selected by this `-k` because its class name contains "Chains". It reports
  `BhavcopyBackfillReport(trading_days=0, days_written=0, bars_written=0, …)` — the calendar had
  **no trading day** for the test's date by the time the backfill read it, which is what a
  concurrent `TRUNCATE … trading_day … CASCADE` from a sibling session's worker suite does
  between this test's own fixture seeding it and its assertion; eight pytest processes were on
  the same Postgres at the time. The decisive check is direct: `git checkout` of
  `services/worker/tests/conftest.py` — the one file of this module's that the test's fixtures
  touch — and the test fails **identically**, `1 failed, 1 passed`, with TW4's thirteen `tw_`
  names removed from the truncation list. It is the arrangement, not the change.

- [x] G8: **The look-ahead test.** Shifting the panel by one session moves every gate, every
      signal and every trigger by exactly one. House rule 5, measured rather than asserted.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests services/api/tests -k "twt and (look_ahead or lookahead or shift)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `41 passed, 5913 deselected in 16.19s`. The fixture is the part that makes this a
  measurement. `04` §3.2's tight test reads **ISO weeks**, so a panel indexed from its start
  changes shape whenever the as-of moves; every panel here is drawn **backwards from its own
  as-of**, and the shape is tight on exactly one session of 260 — every other session is out by at
  least 4 %. Then: panel A ends Friday 2019-09-13, panel B Monday 2019-09-16. Same signals (by
  symbol), same gate, same `pct_above_dma`, same `next_trigger` and `high_since` — and panel B is
  **silent on the Friday**: no signal row, no state row. A detector that had read the following
  Monday's bar would already be calling that Friday tight.
  The second half states it the other way round, which is the form that catches a leak:
  `test_no_look_ahead_a_bar_after_the_session_changes_no_answer_for_it` writes the same panel with
  **one extra session after the as-of**, closing at 500 on 99 million shares, and asserts the
  entire snapshot for the as-of session is byte-identical — the state rows, the signal rows, the
  breadth row, the funnel JSON and the trigger the book was given that evening.

- [x] G9: The 21:00 IST retry is a no-op when the session already has rows, and `make twt DATE=…`
      exists.
  CHECK: cd decile-blueprint && grep -nE "^twt:" Makefile && uv run pytest -k "twt and retry" 2>&1 | tail -3
  EXPECT: /twt:/
  EVIDENCE: `Makefile:171 twt:           ## TW4: detect three-weeks-tight state and signals for one
  date: make twt DATE=2026-09-10 [SESSIONS=5] [FORCE=1]`, and `3 passed, 7469 deselected in 2.35s`.
  The Beat entry is `celery_app.BEAT_SCHEDULE["twt-detect"]` at `crontab(hour=21, minute=0,
  day_of_week="mon-fri")` on the compute queue, which is the time `06` § TW4 names. The Beat task
  and `make twt` share one helper — `tasks.twt.detect_session`, which lives in the job's own
  module rather than the CLI's, because a rule kept where the scheduler does not import it is a
  rule that drifts — so the two cannot come to disagree about when a session counts as done.
  **It asks `tw_breadth_daily`, not `tw_signal_daily`**
  (DECISIONS-TW TW4.3): this book signals about eighteen times a *year*, so a session with no
  signal rows is what a session that ran perfectly looks like on almost every weeknight, and a
  retry keyed on signals would densify 260 sessions over the whole cash universe nightly to arrive
  at the same answer. The test drives exactly that case — a panel whose only name is never tight,
  so no states and no signals — and the retry still answers `{"skipped": "already detected"}`.
  `FORCE=1` re-detects, and the snapshot is unchanged, because the write underneath it is an
  upsert.
  The target was also run for real against the development database:
  `BASKFY_SOLE_USER_ID=1 make twt DATE=1999-01-04` printed
  `{"user_id": 1, "nightly_enabled": true, "sessions": [{"date": "1999-01-04", "signals": 0,
  "status": "skipped", "detail": {"skipped_reason": "no bars for 1999-01-04 in the TWT
  universe"}}]}` — the whole wiring exercised, and **no row written**, which is the right answer
  for a date the plant has never published. Without the variable it refuses by name rather than
  inventing a tenant.

- [x] G10: Both Python suites green and `make lint` clean. Baseline: core `4012 passed, 8
      skipped`; `ruff check .` and `ruff format --check .` clean over 728 files; `mypy --strict`
      Success over 623.
  CHECK: cd decile-blueprint && uv run ruff check . 2>&1 | tail -2 && uv run mypy --strict 2>&1 | tail -2
  EXPECT: /Success|All checks passed/
  EVIDENCE: `make lint` **clean end to end at 21:10 IST**, all four steps: `ruff check .` ->
  `All checks passed!`; `ruff format --check .` -> `737 files already formatted`; `uv run mypy`
  (strict) -> `Success: no issues found in 632 source files`; `pnpm -r run lint` -> both
  workspace projects `Done`, 0 errors (the single `react-hooks/incompatible-library` warning in
  `data-table.tsx` is pre-existing and is a warning).
  The suites, each run in its own process so that nothing in the tally is a deadlock:
  **core `4077 passed, 5 skipped`** (full, db included; `4052 passed, 5 skipped, 25 deselected`
  on the `-m "not db"` half — a second full core run while two sibling suites were hammering the
  same Postgres reported `1 failed, 4076 passed`, and the one failure,
  `test_twt_schema.py::TestTheConstraintsThatAreRulesActuallyRefuse::test_an_order_side_outside_buy_and_sell_is_refused`,
  is TW3's and passes 12/12 when its class is re-run alone — contention, not a regression); **`packages/execution` + `packages/providers` `508 passed`**;
  **`services/api` + `services/worker` `-m "not db"` `1039 passed, 158 skipped`**; the
  **TypeScript** side `165 files / 2933 tests passed`; and the **desk tree**, which this module
  never opens and whose green suite is the safety rail that it can still rebalance on a Friday —
  `1883 passed, 17 skipped, 12 subtests` under `DRY_RUN=true`. Every db-marked test this module
  owns is in G1–G9 above and every one of them ran against the live Postgres.

  **Two things this gate must not claim, and does not.**

  1. **One `services/api` + `services/worker` run in a single process reported
     `308 failed, 2315 passed, 240 errors`, and none of it is this module's.** Three agents share
     one Postgres in this session and sibling sessions were running `pytest services/worker/tests`
     for the whole nineteen minutes; that suite `TRUNCATE ... RESTART IDENTITY CASCADE`s the
     pipeline tables and re-seeds them, while the api tree's own fixtures `DROP SCHEMA public
     CASCADE`. The failures are in `test_public_api`, `test_seed`, `test_swing_schema_and_settings`,
     `test_backtest_job`, `test_holdings_sync`, `test_kite_login_nudge`, `test_swing_catalyst` —
     modules TW4 does not touch and does not import.
     **The proof that it is the arrangement and not the change:** two of the modules that errored
     in that run, `test_holdings_sync.py` and `test_backtest_job.py`, were re-run on their own
     immediately afterwards and gave `42 passed` — and they are exercised through the *same*
     `clean_db` fixture whose `TRUNCATE` list this module extended, so if the thirteen `tw_` names
     added to it were wrong, those are the tests that would say so. Earlier, before the sibling
     runs started, the same contention produced `DeadlockDetectedError` on `INSERT INTO instrument`
     inside this module's own fixture; the fix taken was to stop it writing to a shared table at
     all — it no longer touches `trading_day` — after which every targeted run in a quiet window
     was green, which is what G1-G9's evidence is. `make test-db` runs one process, which is the
     arrangement all of this is written for.
     Run **alone**, `services/worker/tests` still reported `19 failed, 936 passed, 41 errors`
     while a sibling suite was live, and its errors land in `test_vbt_detect`,
     `test_vbt_evening`, `test_vbt_backtest_job` and `test_twt_step` alike — a spread no single
     change produces. Every one of those modules passes when run on its own in a quiet window:
     TW4's own two modules give **`32 passed`** together
     (`services/api/tests/test_twt_detect.py` 24 + `services/worker/tests/test_twt_step.py` 8),
     `test_celery_config.py` `24 passed`, `test_holdings_sync.py` + `test_backtest_job.py`
     `42 passed`, `test_twt_schema.py` + `test_twt_ceilings.py` `176 passed`,
     `test_api_artifacts.py` `12 passed`.
     **So this gate does not claim a clean full-suite number, because it did not get one**, and
     the honest statement is the one above: every suite this module can be held responsible for
     is green when it is the only thing talking to the database.
  2. **`mypy --strict` and `ruff format --check` are red *right now*, in a file this module has
     never opened.** `services/worker/tests/test_deep_backfill.py` gained ~103 lines from a
     concurrent session between 21:10 and 21:22; `git diff` shows both offending lines
     (`add_bar(session, instrument, dt.date(...), Decimal("100"))`, argument 4 typed `str`) as
     additions that are not this module's, and the same file is the only one `ruff format`
     objects to. `ruff check .` is still `All checks passed!`. TW4's own files are clean under
     all four steps, cold cache included.
<!-- A checked box whose EVIDENCE reads "pending" is UNMET. ABANDON: G<n> <reason> is the honest exit. -->
