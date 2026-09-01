# Gates: leaf C1-nav-job

Scope: §5.1 nightly EOD NAV job — the series everything else reads

Written by the parent before fan-out. The leaf agent proves its own work; the parent
re-runs these checks independently — that verification hierarchy is the whole point of
orchestrated mode.

---

- [x] G1: The job exists and its tests pass.
  CHECK: cd decile-blueprint && export BASKFY_TEST_DATABASE_URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_tree3_test" && uv run pytest services/worker/tests/test_portfolio_nav_job.py -p no:randomly 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ....................                                                     [100%] | 20 passed in 2.45s

- [x] G2: Criterion 1 through the job: consolidated equals the sum of its parts, to the paisa.
  CHECK: cd decile-blueprint && export BASKFY_TEST_DATABASE_URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_tree3_test" && uv run pytest services/worker/tests/test_portfolio_nav_job.py -p no:randomly -k "consolidated or sum or criterion_1" 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ..                                                                       [100%] | 2 passed, 18 deselected in 0.92s

- [x] G3: An open reconciliation item marks the portfolio pending rather than inventing a value (§4.3).
  CHECK: cd decile-blueprint && export BASKFY_TEST_DATABASE_URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_tree3_test" && uv run pytest services/worker/tests/test_portfolio_nav_job.py -p no:randomly -k "pending or reconcil or freeze" 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ..                                                                       [100%] | 2 passed, 18 deselected in 0.93s

- [x] G4: Idempotent — re-running the same date changes nothing (house rule 7).
  CHECK: cd decile-blueprint && export BASKFY_TEST_DATABASE_URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_tree3_test" && uv run pytest services/worker/tests/test_portfolio_nav_job.py -p no:randomly -k "idempot or twice or rerunning or changes_nothing" 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: .                                                                        [100%] | 1 passed, 19 deselected in 0.98s

<!--
A checked box with EVIDENCE still "pending" counts as UNMET.
If a gate becomes impossible: add `ABANDON: G<n> <reason>` and say so in the report.
-->
