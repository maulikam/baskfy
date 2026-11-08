# Gates: A3 — portfolio_units.py, capital and weights become unit counts

Scope: the structural half of gap 5. A pure function taking sleeve capital, target weights and a price map, returning whole-unit counts plus the cash remainder. No quote fetching lives here.

- [x] G1: the module exists and imports
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run python -c "import baskfy_core.portfolio_units as u; print('IMPORT_OK'); print(sorted(n for n in dir(u) if not n.startswith('_')))"
  EXPECT: IMPORT_OK
  EVIDENCE: IMPORT_OK | ['Decimal', 'FULL_WEIGHT', 'Final', 'InvalidPriceError', 'Mapping', 'MissingPriceError', 'PriceRefusedError', 'Sequence', 'UnitAllocation', 'UnitLine', 'WEIGHT_QUANTIZE', 'WEIGHT_TOLERANCE

- [x] G2: Law 1 holds — the module fetches nothing; prices arrive as an argument
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && echo "HITS=$(rg -c 'sqlalchemy|httpx|requests|kiteconnect|datetime\.now|utcnow|\bopen\(' packages/core/src/baskfy_core/portfolio_units.py 2>/dev/null || echo 0)"
  EXPECT: HITS=0
  EVIDENCE: HITS=0

- [x] G3: units are whole numbers and never exceed the capital available at the given prices
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest packages/core/tests/test_portfolio_units.py -k "whole or integral or affordable" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ............                                                             [100%] | 12 passed, 48 deselected in 0.07s

- [x] G4: the cash remainder is returned explicitly and capital reconciles — sum(units x price) + remainder == capital
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest packages/core/tests/test_portfolio_units.py -k "remainder or reconcile" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ..........                                                               [100%] | 10 passed, 50 deselected in 0.06s

- [x] G5: a missing price is refused loudly, never silently treated as zero or skipped
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest packages/core/tests/test_portfolio_units.py -k "missing_price or unpriced" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: .......                                                                  [100%] | 7 passed, 53 deselected in 0.06s

- [x] G6: all arithmetic is Decimal; passing a float price is rejected rather than coerced (house rule 9)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest packages/core/tests/test_portfolio_units.py -k "decimal or float" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: .............                                                            [100%] | 13 passed, 47 deselected in 0.06s

- [x] G7: held-units vs target-units drift is expressible — the function reports both, so a sleeve can show what is actually held
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest packages/core/tests/test_portfolio_units.py -k "held or drift or target" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ...........                                                              [100%] | 11 passed, 49 deselected in 0.06s

- [x] G8: zero capital, zero weight and a single-name sleeve are all handled without a crash or a divide-by-zero
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest packages/core/tests/test_portfolio_units.py -k "zero or edge or single" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ...................                                                      [100%] | 19 passed, 41 deselected in 0.07s

- [x] G9: the module's suite passes
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest packages/core/tests/test_portfolio_units.py --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ............................................................             [100%] | 60 passed in 0.07s

- [x] G10: lint + mypy strict clean, no escape hatches
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run ruff check packages/core/src/baskfy_core/portfolio_units.py packages/core/tests/test_portfolio_units.py 2>&1 | tail -2 && uv run mypy packages/core/src/baskfy_core/portfolio_units.py 2>&1 | tail -2
  EXPECT: /Success: no issues/
  EVIDENCE: All checks passed! | Success: no issues found in 1 source file
