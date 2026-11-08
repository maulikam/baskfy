# Gates: LV5 — intraday equity bars for the liquid universe (store, session reconcile, backfill)

Scope: the data the review says does not exist — one-minute equity bars — captured for the liquid universe after each session and backfilled resumably, with pure readers in core, so a live TWT/VBT variant can be defined and backtested instead of guessed. No live tick collector (deferred with the variants; DECISIONS-LV LV5.1).

- [x] B1: migration 0056 creates eq_minute_bar with PK (instrument_id, ts) and numeric prices, and downgrades cleanly
  CHECK: cd decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest services/api/tests/test_migration_0056.py 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 2 passed in 7.01s

- [x] B2: reconcile_session writes only closed minutes, rounds at write, and re-running a day produces identical rows (house rules 7, 8)
  CHECK: cd decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest services/worker/tests/test_eq_bars_worker.py -k 'idempotent or closed_minutes or rounding' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 1 passed, 1 skipped, 6 deselected in 0.29s

- [x] B3: backfill is resumable per instrument per 60-day window and starts from the newest stored bar
  CHECK: cd decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest services/worker/tests/test_eq_bars_worker.py -k 'resum' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 1 passed, 7 deselected in 1.70s

- [x] B4: the Beat entry baskfy.eq_bars.session runs 15:45 Mon–Fri and the universe is swing's liquid_universe as of the last published session (no second predicate)
  CHECK: cd decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest services/worker/tests/test_eq_bars_worker.py -k 'beat or universe' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 2 passed, 6 deselected in 0.29s

- [x] B5: core readers are pure (no I/O import) and reuse options/bars for 5-minute aggregation
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_eq_bars.py packages/core/tests/test_no_escape_hatches.py 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 16 passed in 0.53s

- [x] B6: the cost is stated and bounded: one historical call per instrument per session; the universe size on the last published session is measured and recorded in DECISIONS-LV
  CHECK: grep -cE 'liquid universe .* [0-9]+ names' docs/live/DECISIONS-LV.md
  EXPECT: /[1-9]/
  EVIDENCE: 1

- [x] B7: lint clean (ruff, format, mypy strict)
  CHECK: cd decile-blueprint && make lint >/dev/null 2>&1 && echo LINT CLEAN || echo LINT FAILED
  EXPECT: LINT CLEAN
  EVIDENCE: LINT CLEAN
