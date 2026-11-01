# Gates: F3-7 — the web card and F3 in the quarterly re-test

- [x] W1: `retest.FAMILIES` carries `F3N` and `F3B` (scope `DIRECTIONAL`), run on an index's raw rows with the weeklies; an F3 family without the raw loader is refused, not run on the panel
  CHECK: cd decile-blueprint && uv run pytest -q packages/core/tests/test_fno_retest.py packages/core/tests/test_fno_directional_retest.py
  EXPECT: /passed/
  EVIDENCE: 39 passed, 6 skipped (the golden needs BASKFY_FNO_RESEARCH_DIR)

- [x] W2: the trim changes no trade (the proxy's trades with and without `trim_index_contracts` are equal on the fixture) and the worker's loader keeps the weeklies the option panel drops
  CHECK: cd decile-blueprint && uv run pytest -q packages/core/tests/test_fno_directional_retest.py -k "trim" && grep -c "test_f3_reads_the_weeklies_the_option_panel_drops" services/worker/tests/test_fno_retest_run.py
  EXPECT: /1/
  EVIDENCE: 2 passed; the worker test is DB-backed and was not run in the cloud container (no TimescaleDB)

- [x] W3: `GET /fno/overnight` serves `f3` (per underlying, open, closed, evidence, not_tested, backtests) and an `F3` gate; openapi.json and the generated client are current
  CHECK: cd decile-blueprint && uv run pytest -q services/api/tests/test_api_artifacts.py
  EXPECT: /passed/
  EVIDENCE: artifacts green after `make client`; test_api_fno's F3 assertions are DB-backed and were not run here

- [x] W4: `/options/overnight` draws the F3 section read-only (two cards, the proposed spread, open spreads marked at settle, the exits in words, the untested rules, the re-test), and the web suite and lint are green
  CHECK: cd decile-blueprint/apps/web && npx vitest run && pnpm lint
  EXPECT: /passed/
  EVIDENCE: Test Files 235 passed (235), Tests 3591 passed (3591); lint 0 errors (1 pre-existing warning in info-table)
