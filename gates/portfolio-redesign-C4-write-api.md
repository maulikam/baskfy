# Gates: leaf C4-write-api

Scope: the two endpoints the onboarding flow needs: suggestions and create

Written by the parent. The leaf proves its own work; the parent re-runs these independently.

---

- [x] G1: Both endpoints exist and their tests pass.
  CHECK: cd decile-blueprint && export BASKFY_TEST_DATABASE_URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_tree3_test" && uv run pytest services/api/tests/test_portfolio_write.py -p no:randomly 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ..................                                                       [100%] | 18 passed in 1.87s

- [x] G2: Criterion 2 at the write boundary: allocating a holding already in a capital portfolio is refused clearly.
  CHECK: cd decile-blueprint && export BASKFY_TEST_DATABASE_URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_tree3_test" && uv run pytest services/api/tests/test_portfolio_write.py -p no:randomly -k "conflict or already or criterion_2 or duplicate" 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: .                                                                        [100%] | 1 passed, 17 deselected in 0.59s

- [x] G3: §4.2 is structural: the request schema has no quantity field at all.
  CHECK: cd decile-blueprint && export BASKFY_TEST_DATABASE_URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_tree3_test" && uv run pytest services/api/tests/test_portfolio_write.py -p no:randomly -k "quantity or whole" 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ..                                                                       [100%] | 2 passed, 16 deselected in 0.06s

- [x] G4: The checked-in OpenAPI artifact carries the new routes.
  CHECK: cd decile-blueprint && export BASKFY_TEST_DATABASE_URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_tree3_test" && uv run pytest services/api/tests/test_api_artifacts.py -p no:randomly 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ............                                                             [100%] | 12 passed in 2.16s

<!--
A checked box with EVIDENCE still "pending" counts as UNMET.
If a gate becomes impossible: add `ABANDON: G<n> <reason>` and say so in the report.
-->
