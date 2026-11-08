# Gates: 2.A — stored ranking factors (contract C1)

Scope: every C1 column computed in core (pure), rounded at write time, on the ORM, migrated, registered with weight_family/rankable/validation_status/definition, and pinned by spec tests.

- [x] G1: New pure module computes every C1 per-instrument factor, called from `compute_factors_unrounded`; spec tests pass (hand-computed expectations on small synthetic series for EVERY column, incl. NULL-when-window-short and point-in-time: appending a future bar never changes an earlier row).
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_factors_ranking.py 2>&1 | tail -3
  EXPECT: /\d+ passed(?!.*failed)/
  EVIDENCE: 2026-09-13 uv run pytest packages/core/tests/test_factors_ranking.py --color=no -> 150 passed in 2.46s (re-run after restoring mom_pctile (r-1)/(n-1); 4 TestMomentumPercentile tests had gone red)

- [x] G2: Existing factor suites still green (golden, crossvalidation, edge guards, properties, benchmark, registry, precision).
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_factors_golden.py packages/core/tests/test_factor_crossvalidation.py packages/core/tests/test_factor_edge_guards.py packages/core/tests/test_factor_properties.py packages/core/tests/test_factor_benchmark.py 2>&1 | tail -3
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: ..                                                                       [100%] | 146 passed in 55.88s

- [x] G3: Every C1 column is on `FactorDaily`, in `COLUMN_PRECISION`/`INTEGER_COLUMNS` (no unpriced numeric column), and in migration `0046_ranking_factors` (down_revision `0045_instrument_watch_and_prefs`).
  CHECK: cd decile-blueprint && for c in atr_14 atr_ext_20 ma50_slope_20 eff_ratio_63 max_dd_6m max_dd_12m downside_vol_6m downside_vol_12m sortino_6m sortino_12m underwater_12m ret_ex_top3_12m accel_21_105 accel_21_105_vs vol_exp_21_126 vol_persist_20 excess_ret_3m excess_ret_6m excess_ret_12m resid_ret_12m rs_persist_126 mom_pctile rank_persist_20 nse_mr6 nse_mr12; do for f in packages/core/src/baskfy_core/models/facts.py packages/core/src/baskfy_core/precision.py services/api/alembic/versions/0046_ranking_factors.py; do grep -q "\b$c\b" $f || echo "MISSING $c in $f"; done; done; echo scan-done
  EXPECT: /^scan-done\s*$/
  EVIDENCE: scan-done

- [x] G4: Migration upgrades and downgrades cleanly on a scratch DB (0045 → 0046 → 0045 → 0046).
  EVIDENCE: 2026-09-13 scratch DB baskfy_scratch_a, BASKFY_DATABASE_URL=...5433/baskfy_scratch_a: alembic upgrade 0045 (C1 cols=0) -> upgrade 0046 (current 0046, 25/25 C1 cols in factor_daily) -> downgrade 0045 (cols=0) -> upgrade 0046 (25/25); DB dropped after

- [x] G5: Registry: every C1 rankable key + `regime_priority` present with the C1 preference; `excess_ret_*`, `mom_pctile`, `nse_mr*`, `atr_14` have rankable=False; every factor (old and new) has a weight_family and a non-empty definition; pre-Phase-2 factors are `legacy`, new ones `research`; `sql_for` works for each SQL key. Registry tests green.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests -k "registry" 2>&1 | tail -3
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: ....................................................                     [100%] | 484 passed, 4564 deselected in 2.44s

- [x] G6: `ranking_research.py` no longer carries second formulas: it delegates to (or is replaced by) the stored implementations, and its tests still pass or were rewritten against the spec.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_ranking_research.py 2>&1 | tail -2
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: ..... [100%] | 5 passed in 48.18s

- [x] G7: Law 1 + escape hatches + ruff + mypy clean for packages/core.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_no_escape_hatches.py 2>&1 | tail -1 && uv run ruff check packages/core services/api/alembic && uv run ruff format --check packages/core services/api/alembic && uv run mypy 2>&1 | tail -1
  EXPECT: /Success: no issues found/
  EVIDENCE: 321 files already formatted | Success: no issues found in 713 source files

- [x] G8: docs/04-data-model.md lists the new columns (and the docs-schema test passes); DECISIONS-MERGE.md has a "Ranking 2.A" entry for every judgement call (benchmark choice, ddof, month-end anchoring).
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests -k "schema_matches_docs or docs" 2>&1 | tail -2; grep -c "Ranking 2.A" ../docs/DECISIONS-MERGE.md
  EXPECT: /passed[\s\S]*\n[1-9]/
  EVIDENCE: 709 passed, 4339 deselected in 1.77s | 1
