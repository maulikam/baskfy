# Gates: leaf C3-api

Scope: §6/§7 the read API behind the redesigned pages

Written by the parent before fan-out. The leaf agent proves its own work; the parent
re-runs these checks independently — that verification hierarchy is the whole point of
orchestrated mode.

---

- [x] G1: The routes exist and their tests pass.
  CHECK: cd decile-blueprint && export BASKFY_TEST_DATABASE_URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_tree3_test" && uv run pytest services/api/tests/test_portfolio_overview.py -p no:randomly 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ..............................                                           [100%] | 30 passed in 4.14s

- [x] G2: The checked-in OpenAPI document and TS client carry the new routes — a served route absent from the artifact is a surface nobody agreed to.
  CHECK: cd decile-blueprint && export BASKFY_TEST_DATABASE_URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_tree3_test" && uv run pytest services/api/tests/test_api_artifacts.py -p no:randomly 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ............                                                             [100%] | 12 passed in 3.20s

- [x] G3: Totals exclude monitoring views, and every return field is labelled (criteria 2, 3, 5).
  CHECK: cd decile-blueprint && export BASKFY_TEST_DATABASE_URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_tree3_test" && uv run pytest services/api/tests/test_portfolio_overview.py -p no:randomly -k "monitoring or label or model" 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ........                                                                 [100%] | 8 passed, 22 deselected in 1.61s

- [x] G4: Tenancy holds — another user's portfolio 404s rather than leaking its existence.
  CHECK: cd decile-blueprint && export BASKFY_TEST_DATABASE_URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_tree3_test" && uv run pytest services/api/tests/test_portfolio_overview.py -p no:randomly -k "tenan or foreign or other_user or 404" 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ...                                                                      [100%] | 3 passed, 27 deselected in 0.97s

<!--
A checked box with EVIDENCE still "pending" counts as UNMET.
If a gate becomes impossible: add `ABANDON: G<n> <reason>` and say so in the report.
-->
