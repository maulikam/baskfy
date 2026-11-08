# Gates: 2.E — portfolio-aware selection (contract C5)

Scope: pure `select_portfolio` with retention/entry, sector cap, capacity, turnover budget, correlation; quality scores immutable; informational only.

- [x] G1: Spec tests for each rule in C5 order: hold ≤ retention_rank, exit reasons, entry ≤ entry_rank, SECTOR_CAP, CAPACITY (value/adv > pct), CORRELATION (max |corr| to any holding over window > limit, using returns frame), TURNOVER_BUDGET (entries+exits capped, exits of worst-ranked first), FULL, proposed value/qty/participation arithmetic, deterministic tie order.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_ranking_selection.py 2>&1 | tail -2
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: [32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m

- [x] G2: A property test proves candidate scores and quality ranks are byte-identical before/after selection for random inputs (hypothesis or seeded loop ≥ 200 cases).
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_ranking_selection.py -k "immutable or unchanged" 2>&1 | tail -2
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: [32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m                                                                     [100%][0m | [32m[32m[1m4 passed[0m, [33m55 deselected[0m[32m in 2.02s[0m[0m

- [x] G3: Nothing in the module can reach the order path.
  CHECK: cd decile-blueprint && grep -nE "place_order|OrderGateway|baskfy_execution|plan_id" packages/core/src/baskfy_core/ranking_selection.py | wc -l
  EXPECT: /^\s*0\s*$/
  EVIDENCE: 0

- [x] G4: ruff, format, mypy clean; existing rank_buffer tests green.
  CHECK: cd decile-blueprint && uv run ruff check packages/core/src/baskfy_core/ranking_selection.py packages/core/tests/test_ranking_selection.py && uv run ruff format --check packages/core/src/baskfy_core/ranking_selection.py packages/core/tests/test_ranking_selection.py && uv run mypy packages/core/src/baskfy_core/ranking_selection.py packages/core/tests/test_ranking_selection.py 2>&1 | tail -1 && uv run pytest -p no:cacheprovider --color=no packages/core/tests -k rank_buffer 2>&1 | tail -1
  EXPECT: /Success: no issues found[\s\S]*passed/
  EVIDENCE: Success: no issues found in 2 source files | 31 passed, 5017 deselected in 1.57s
