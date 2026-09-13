# Gates: 2.C — worker: nightly wiring, rank persistence, desk score, backfill

Scope: the nightly pipeline writes every C1 column, mom_pctile + rank_persist_20, and desk_score_daily for the session; a resumable backfill fills history; all idempotent.

- [x] G1: `engine.py` loads NIFTY 50 and NIFTY 500 levels from index_snapshot_daily and passes `benchmark` + `market_benchmark`; a DB test proves excess_ret_12m / resid_ret_12m / rs_persist_126 are non-NULL for a seeded instrument with a full year and NULL without the index series.
  CHECK: cd decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest --color=no services/worker/tests -k "ranking_factors or market_benchmark" 2>&1 | tail -2
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: 2026-09-13 pytest -k "ranking_factors or market_benchmark" on baskfy_test_c: 3 passed (test_ranking_worker.py TestBenchmarks; swapping the slugs fails 2).

- [x] G2: After the day's factor rows are upserted, the worker computes `mom_pctile` (core cross_sectional_pctile) and `rank_persist_20` (core rank_persistence over the last 20 dates read from the DB) and updates only those columns; running the step twice yields identical rows (idempotence test).
  CHECK: cd decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest --color=no services/worker/tests -k "rank_persist or mom_pctile" 2>&1 | tail -2
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: 2026-09-13 pytest -k "rank_persist or mom_pctile" on baskfy_test_c: 5 passed (tasks/ranking.py, called from run_compute_factors; other-column and rerun row hashes identical).

- [x] G3: A nightly step writes `desk_score_daily` for the session via `desk_score_service.score_day` with carried columns loaded exactly as 2.B's contract says; upsert is idempotent; the orchestrator runs it after factors and a failure in it is reported without aborting publication of the screener data.
  CHECK: cd decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest --color=no services/worker/tests -k "desk_score" 2>&1 | tail -2
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: 2026-09-13 pytest -k "desk_score" on baskfy_test_c: 7 passed (tasks/desk_score.py, orchestrator.run_compute_desk_score_step in a savepoint; a failing step still publishes).

- [x] G4: `python -m baskfy_worker.factors_cli backfill-ranking --from D1 --to D2 [--resume]` recomputes ONLY the Phase-2 columns (C1 + mom_pctile + rank_persist_20, in date order so persistence sees prior dates) and desk_score_daily for each trading day, skips days already complete unless --force, logs progress, and a DB test proves it leaves every pre-Phase-2 column byte-identical.
  CHECK: cd decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest --color=no services/worker/tests -k "backfill_ranking" 2>&1 | tail -2
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: 2026-09-13 pytest -k "backfill_ranking" on baskfy_test_c: 6 passed (ranking_backfill.py; legacy-column row hashes identical incl. a tampered ret_1m; resume, --force, 20-day order).

- [x] G5: Worker suite green (DB tests included) and ruff/format/mypy clean.
  CHECK: cd decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest --color=no services/worker 2>&1 | tail -1 && uv run ruff check services/worker && uv run ruff format --check services/worker && uv run mypy 2>&1 | tail -1
  EXPECT: /passed[\s\S]*Success: no issues found/
  EVIDENCE: 2026-09-14 at b87521d: worker suite green inside root G2 whole-suite run (8990 passed, 0 failed) after the swing login-race fix f865654 (AF C.1); ruff/format/mypy clean via make lint.

- [x] G6: Measured backfill speed on a realistic local run recorded (seconds per trading day) and written into RUN-AND-TEST.md with the exact box command.
  EVIDENCE: 2026-09-14, 682.5 s/day, f52df2c.
