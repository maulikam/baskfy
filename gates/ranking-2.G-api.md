# Gates: 2.G — API (C6)

- [x] G1: Screen runs use the ranking engine when ranking_terms is non-empty (preview + saved run), legacy path otherwise; DB tests cover composite, sequential, fixed vs filtered scope giving different ranks, within_sector, factor_ranges, desk_score from desk_score_daily with rejected rows absent.
  CHECK: cd decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest --color=no services/api/tests -k "ranking or desk_score" 2>&1 | tail -1
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: 2026-09-13 (with BASKFY_REDIS_URL=redis://localhost:6380/3 -p no:cacheprovider) pytest services/api/tests -k "ranking or desk_score" -> 135 passed, 1980 deselected in 24.26s
  PROGRESS (13 Sep 2026): `execute_screen` dispatches ranking_terms -> build_ranking_frame_query + rank_frame, legacy SQL otherwise; API survivors re-scoring removed (legacy desk_score ranks stored score in SQL, A-F attached from desk_score_daily). DB tests written in test_ranking_screener_db.py (composite, sequential, fixed vs filtered, within_sector, factor_ranges, 3x desk_score) + TestRankingTermsOverHttp (preview, saved run) in test_api_run.py; not yet run (Postgres off).

- [x] G2: Every payload carries `provenance`; contract tests (test_api_run exact keys, client-contract.test.ts) updated and green; Investing 001 parity test still green.
  CHECK: cd decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest --color=no services/api/tests/test_api_run.py services/api/tests/test_screener_db.py 2>&1 | tail -1
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: 2026-09-13 (with BASKFY_REDIS_URL=redis://localhost:6380/3 -p no:cacheprovider) pytest services/api/tests/test_api_run.py services/api/tests/test_screener_db.py -> 200 passed in 19.46s (Investing 001 parity incl.; client-contract.test.ts green per PROGRESS, not re-run: no node today)
  PROGRESS (13 Sep 2026): payload = ScreenResult.payload() + `provenance` (ScreenProvenanceOut); test_api_run exact keys + provenance test, client-contract.test.ts ExpectedProvenance updated; `make client` run; api-client 177 passed, lint clean; web tsc clean. DB run of test_api_run/test_screener_db (Investing 001 parity) still owed.

- [x] G3: `POST /screens/explain`, `POST /screens/selection` (owner-checked portfolio or inline holdings; correlation returns loaded from ohlcv_daily), `GET /meta/ranking-presets`, FactorOut new fields — each with API tests incl. auth/ownership refusals and 422s.
  CHECK: cd decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest --color=no services/api/tests -k "explain or selection or presets or meta" 2>&1 | tail -1
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: 2026-09-13 (with BASKFY_REDIS_URL=redis://localhost:6380/3 -p no:cacheprovider) pytest services/api/tests -k "explain or selection or presets or meta" -> 58 passed, 2084 deselected in 15.82s (test_api_screens_explain_selection.py 27 incl. 401/404-ownership/422/400 refusals and the no-order-path checks)
  PROGRESS (13 Sep 2026): FactorOut gains rankable/preference/weight_family/validation_status/definition (core enums) and `GET /meta/ranking-presets` (public, like /meta/factors) are in; test_api_meta_ranking.py 6 passed without DB; test_api_meta.py factor + preset HTTP tests owed a DB run. /screens/explain and /screens/selection not started. DB run 13 Sep: -k "explain or selection or presets or meta" -> 31 passed (meta + presets only; explain/selection routes do not exist), so G3 stays open.

- [x] G4: No execute/order route added; the safety test that forbids web execute routes still green.
  CHECK: cd decile-blueprint && git grep -nE "place_order|OrderGateway" -- services/api/src/baskfy_api/routers/screens.py | wc -l
  EXPECT: /^\s*0\s*$/
  EVIDENCE: 0

- [ ] G5: `make client` regenerated openapi.json + generated/schema.ts; `generate:check` clean; api-client tests + lint green; full API suite, ruff, format, mypy clean.
  CHECK: cd decile-blueprint && pnpm --filter @baskfy/api-client run generate:check 2>&1 | tail -1 && pnpm --filter @baskfy/api-client test 2>&1 | grep -E "Tests" | tail -1 && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest --color=no services/api 2>&1 | tail -1 && uv run mypy 2>&1 | tail -1
  EXPECT: /passed[\s\S]*Success: no issues found/
  EVIDENCE: pending
  PROGRESS (13 Sep 2026): `make client` run for the FactorOut/presets change (regeneration byte-stable; `openapi --check` clean); api-client tests 176 passed, lint clean after client-contract ExpectedFactor update. `generate:check` exits 1 only because src/generated is uncommitted (it is a git diff). Full API suite owes a DB run; remaining ruff format drift in twt.py, vbt.py, settings.py, test_live_prices.py.
  PROGRESS (13 Sep 2026, DB run): full `pytest services/api` single process -> 2108 passed, 5 failed, 1 skipped, 1 xfailed (9m27s). Ranking-caused failures fixed: test_screener_db sweep (fixture lacked C1 stored factors / monotone ma_dist + vol_expansion; ordinal + computed keys moved to dedicated tests; ma_stack_score SQL now NULL when an MA is missing) and test_api_artifacts (/meta/ranking-presets added to EXPECTED_PATHS + docs/07). Remaining 4 failures are outside ranking: test_bonds_portfolio x3 (gateway guard did not raise; sole-tenant account missing) and test_curated_costs (live_prices mock returns a coroutine).
