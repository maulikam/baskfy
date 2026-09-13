# Gates: 2.D — definition, ranking engine, NSE momentum, screener SQL, presets (C3, C4, C7)

- [x] G1: ScreenDefinition C3 fields + validators in py; TS zod mirror (incl. the desk_score rules the TS side lacked); JSON schema + corpus regenerated with new cases; py/ts parity tests green.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_screen_definition.py packages/core/tests/test_screen_definition_parity.py 2>&1 | tail -1 && pnpm --filter @baskfy/api-client test 2>&1 | grep -E "Tests|passed|failed" | tail -2
  EXPECT: /passed[\s\S]*passed/
  EVIDENCE: [2m Test Files [22m [1m[32m2 passed[39m[22m[90m (2)[39m | [2m      Tests [22m [1m[32m176 passed[39m[22m[90m (176)[39m

- [x] G2: Hash stability: every corpus definition that existed at 999bf37 hashes identically after the change (hashes computed from `git show 999bf37:...` checked into the test).
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests -k "hash_stab" 2>&1 | tail -1
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: [32m[32m[1m16 passed[0m, [33m4528 deselected[0m[32m in 2.95s[0m[0m

- [x] G3: `ranking_engine` implements C4 steps 1–6; spec tests with hand-computed expectations cover: each preference transform incl. ties and n=1, target range inside/outside/one-bound, each missing_data policy, family weighting (three correlated momentum terms do not triple momentum's share), all three scopes (fixed vs filtered produce different percentiles on the same frame; within_sector groups), sequential ≠ composite on a constructed frame, deterministic tie order, contributions sum to composite_score.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_ranking_engine.py 2>&1 | tail -1
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: [32m[32m[1m52 passed[0m[32m in 0.64s[0m[0m

- [x] G4: `nse_momentum` reproduces the NSE text: tests on a hand-worked 5-stock example (MR, Z per horizon, 50/50 weight, normalisation branch for Z<0), eligibility (not in Nifty 200 / no F&O / short history ⇒ NULL), month-end anchoring.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_nse_momentum.py 2>&1 | tail -1
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: [32m[32m[1m19 passed[0m[32m in 0.45s[0m[0m

- [x] G5: `explain` builds the full RankExplanation (terms, positives, deductions, eligibility failures, data quality, desk block, provenance) — tested for a passing row, a failing row, a desk-rejected row, and a missing-data row.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_ranking_engine.py -k explain 2>&1 | tail -1
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: [32m[32m[1m9 passed[0m, [33m43 deselected[0m[32m in 0.50s[0m[0m

- [x] G6: screener.py: (a) legacy sequential orders by factor values, not unique row numbers (test that r2 decides a tie in r1's value); (b) `build_ranking_frame_query` returns the pre-filter universe with `passes_filters`, per-clause `fail__*` flags, PIT narrowest-sector slug, desk_score_daily join, NSE inputs; (c) factor_ranges and regime_in clauses; (d) desk_score legacy path reads desk_score_daily and never ranks rejected rows; (e) default-definition SQL for legacy screens unchanged (existing screener SQL-shape tests green).
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_screener.py packages/core/tests/test_ranking_semantics.py packages/core/tests/test_desk_score_screen.py packages/core/tests/test_ranking_frame_query.py 2>&1 | tail -1
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: 2026-09-13 `cd decile-blueprint && uv run pytest --color=no packages/core/tests/test_screener.py packages/core/tests/test_ranking_semantics.py packages/core/tests/test_desk_score_screen.py packages/core/tests/test_ranking_frame_query.py 2>&1 | tail -1` -> 232 passed in 1.63s (behaviour via in-memory SQLite; PostgreSQL run of these paths is 2.G G1)

- [x] G7: Presets C7 all validate as ScreenDefinition patches over the default definition; `nse_momentum` no longer refused; statuses present.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_ranking_presets.py 2>&1 | tail -1
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: [32m[32m[1m5 passed[0m[32m in 0.87s[0m[0m

- [x] G8: Full core suite, ruff, format, mypy, namespace check clean.
  CHECK: cd decile-blueprint && uv run pytest packages/core -x -p no:cacheprovider 2>&1 | tail -1 && uv run ruff check packages && uv run ruff format --check packages && uv run mypy 2>&1 | tail -1 && bash ../tools/check-namespace.sh 2>&1 | tail -1
  EXPECT: /passed[\s\S]*Success: no issues found/
  EVIDENCE: 2026-09-13 `uv run pytest packages/core --color=no -p no:cacheprovider` (single process) -> 5064 passed, 5 skipped, 25 errors in 238.42s; all 25 errors are test_twt_schema.py DB-fixture setup (alembic upgrade head: connection refused on localhost:5433, Docker/Postgres not running), no failures. Fixed test_momentum_scan::test_the_bytes_are_stable (Polars divide-by-literal landed one ulp off k/10^p after 2.A's frame changes; precision.round_expr now snaps with .round(places)). `ruff check packages/core` -> All checks passed!; `ruff format --check packages/core` -> 276 files already formatted; `mypy packages/core` -> Success: no issues found in 278 source files; `bash tools/check-namespace.sh` -> OK, exit 0

- [x] G9: DECISIONS-MERGE "Ranking 2.D" entry (scope default split, missing-data default, NSE ddof, within_sector groups over the selected universe, unclassified sector bucket).
  CHECK: grep -c "Ranking 2.D" docs/DECISIONS-MERGE.md
  EXPECT: /[1-9]/
  EVIDENCE: 2026-09-13 `grep -c "Ranking 2.D" docs/DECISIONS-MERGE.md` -> 1 (entry 2D.1-2D.7 plus a Ranking 2.F entry, both UNREVIEWED)
