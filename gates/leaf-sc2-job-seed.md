# Gates: SC2 metrics job and first SCAN projection

Scope: Persist EOD metrics idempotently, schedule them after publish, and deterministically seed the first SCAN basket.

- [x] G1: The metrics service writes exact SC2 metrics for a fixture basket and rerun is a row/content no-op
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc_job_test uv run pytest services/worker/tests/test_curated_metrics_job.py -q -rA -k 'metrics and idempotent'
  EXPECT: /PASSED /
  EVIDENCE: 2026-08-23 — exact command passed 1/1; fixture pins min_amount=432.00 and since-inception=5.4189511451, then proves a rerun writes zero rows and preserves computed_at/content.

- [x] G2: Job processing is set-based/bounded and missing price coverage fails honestly without partial writes
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc_job_test uv run pytest services/worker/tests/test_curated_metrics_job.py -q -rA -k 'bounded or coverage or rollback or query_count'
  EXPECT: /PASSED /
  EVIDENCE: 2026-08-23 — exact command passed 6/6: missing/late coverage leaves zero metrics, the streamed-row and pre-SQL interval caps fail before writes, 15y x 50 fixed names estimates 200,000 rows under the 250,000 cap, and 20 baskets share 14 price rows in <=7 statements/<2s.

- [x] G3: Beat runs curated metrics after nightly publish on weekdays and routes it to compute
  CHECK: cd decile-blueprint && uv run pytest services/worker/tests/test_curated_metrics_job.py -q -rA -k 'beat or route or schedule'
  EXPECT: /PASSED /
  EVIDENCE: 2026-08-23 — exact no-DB command passed 3/3: weekday 20:20 recovery schedule, compute routing/binding, successful late publish causally dispatches the exact date while a failed pipeline dispatches nothing.

- [x] G4: Fixture MomentumScan projects deterministically to one SCAN basket/genesis version with exact weights
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc_job_test uv run pytest services/worker/tests/test_curated_scan_projection.py -q -rA
  EXPECT: /PASSED /
  EVIDENCE: 2026-08-23 — exact command passed 8/8; one private valid source screen/run, one SCAN basket/genesis, 0.3334/0.3333/0.3333 largest-remainder weights totaling exactly 1.0000, plus provenance/malformed/cap refusals.

- [x] G5: Reseeding/projection changes no existing rows and never creates an order, credential, subscription, or payment
  CHECK: cd decile-blueprint && DRY_RUN=true BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc_job_test uv run pytest services/worker/tests/test_curated_scan_projection.py -q -rA -k 'idempotent or forbidden or no_order'
  EXPECT: /PASSED /
  EVIDENCE: 2026-08-23 — exact command passed 1/1; second projection returns all-zero writes, a full row/timestamp snapshot is identical, and eight order/fee/subscription/payment/auth-token tables remain empty.

- [x] G6: Expert reread, failure recovery, performance/memory pass, and free polish find no remaining job/seed defect
  EVIDENCE: 2026-08-23 — four passes completed. Expert reread separated adjusted returns from raw affordability and made the scan source a sole-user-private, schema-valid screen. Defect hunt fixed late-publish loss, O(baskets×days×global instruments) scanning, global ever-held history loading, false late-start history, and same-date timestamp rewrites. Failure recovery computes every basket before mutation and covers removed→re-added volatility. Security review found no provider/execution import or network/order call. Final six-file ruff/format/mypy pass is green.
